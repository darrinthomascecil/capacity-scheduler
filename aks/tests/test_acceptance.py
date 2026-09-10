"""
The eight acceptance criteria from DESIGN.md section 9, one test class each.

Everything here runs offline: no Azure, no model, no network.
"""

import datetime as _dt
import json
import os
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aksprofile import bounds as bounds_mod          # noqa: E402
from aksprofile import executor, worker              # noqa: E402
from aksprofile.model import (                       # noqa: E402
    ProfileError, clamp, describe, resolve, validate,
)
from aksprofile.store import MemoryStore             # noqa: E402

LA = ZoneInfo("America/Los_Angeles")


def example_profile():
    """The DESIGN.md section 1.1 / section 3 record, verbatim."""
    return {
        "name": "apps-business-hours",
        "target": {"subscription": "sub", "resourceGroup": "rg",
                   "cluster": "cluster1", "nodePool": "apps"},
        "timezone": "America/Los_Angeles",
        "mode": "count",
        "windows": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                     "start": "06:00", "end": "20:00", "value": 4}],
        "otherwise": 1,
        "endDate": None,
        "paused": False,
        "sourceText": "Scale to 4 nodes every weekday at 6am, then scale down to 1 at 8pm.",
    }


def at(local_str, tz=LA):
    return _dt.datetime.strptime(local_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz)


# --------------------------------------------------------------- AC1 --------

class AC1_ExampleRoundTrip(unittest.TestCase):
    """Creating the section 1.1 example yields the section 3 record and prints
    the section 1.1 table."""

    def test_record_validates(self):
        self.assertIsNotNone(validate(example_profile()))

    def test_resolves_to_the_table(self):
        p = example_profile()
        cases = [
            ("2026-09-14 05:59", 1),   # Mon, before open
            ("2026-09-14 06:00", 4),   # Mon, open
            ("2026-09-14 19:59", 4),   # Mon, last minute
            ("2026-09-14 20:00", 1),   # Mon, closed
            ("2026-09-18 12:00", 4),   # Fri midday
            ("2026-09-19 12:00", 1),   # Sat -> weekend
            ("2026-09-20 12:00", 1),   # Sun -> weekend
            ("2026-09-21 12:00", 4),   # following Mon
        ]
        for local, expected in cases:
            with self.subTest(local=local):
                self.assertEqual(resolve(p, at(local))["value"], expected)

    def test_report_matches_the_table(self):
        text = describe(example_profile())
        self.assertIn("Target: cluster1 / apps", text)
        self.assertIn("Monday-Friday, 06:00-20:00: 4 nodes", text)
        self.assertIn("Otherwise: 1 nodes", text)
        self.assertIn("None - repeat until paused, changed, or deleted", text)


# --------------------------------------------------------------- AC2 --------

class AC2_MissingScaleDownIsRejected(unittest.TestCase):
    """'Scale up to four nodes every weekday at 6am' -- no scale-down -- must be
    a question, not a saved profile (F12)."""

    def test_model_reporting_missing_raises(self):
        from aksprofile.interpret import NeedsClarification, to_profile
        proposal = {
            "windows": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                         "start": "06:00", "end": "20:00", "value": 4}],
            "otherwise": None, "mode": "count",
            "timezone": "America/Los_Angeles",
            "missing": ["scale_down_value"], "notes": "",
        }
        with self.assertRaises(NeedsClarification) as ctx:
            to_profile("p", "scale up to four nodes every weekday at 6am",
                       example_profile()["target"], proposal)
        self.assertIn("scale down to", " ".join(ctx.exception.questions).lower())

    def test_absent_otherwise_is_caught_even_if_model_forgets_to_flag_it(self):
        """The rule is enforced in code, not left to the model's judgement."""
        from aksprofile.interpret import NeedsClarification, to_profile
        proposal = {
            "windows": [{"days": ["Mon"], "start": "06:00", "end": "20:00", "value": 4}],
            "otherwise": None, "mode": "count",
            "timezone": "America/Los_Angeles",
            "missing": [], "notes": "",
        }
        with self.assertRaises(NeedsClarification):
            to_profile("p", "text", example_profile()["target"], proposal)

    def test_missing_timezone_is_caught(self):
        from aksprofile.interpret import NeedsClarification, to_profile
        proposal = {
            "windows": [{"days": ["Mon"], "start": "06:00", "end": "20:00", "value": 4}],
            "otherwise": 1, "mode": "count", "timezone": "",
            "missing": [], "notes": "",
        }
        with self.assertRaises(NeedsClarification) as ctx:
            to_profile("p", "text", example_profile()["target"], proposal)
        self.assertIn("timezone", " ".join(ctx.exception.questions).lower())


# --------------------------------------------------------------- AC3 --------

class AC3_SurvivesRestart(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_paused_state_is_stored_not_held_in_a_variable(self):
        store = MemoryStore()
        store.save(example_profile())
        store.set_paused("apps-business-hours", True)
        got = store.get("apps-business-hours")
        self.assertTrue(got["paused"])
        self.assertEqual(got["windows"][0]["value"], 4)

    def test_only_a_durable_backend_may_be_deployed(self):
        """F5 is satisfied by the backend, not by the app. The memory backend
        declares that it cannot, so nothing can deploy it by accident."""
        from aksprofile.store import TableStore
        self.assertFalse(MemoryStore.durable)
        self.assertTrue(TableStore.durable)

    def test_there_is_no_silent_default_store(self):
        import os as _os
        from aksprofile.store import open_store
        saved = _os.environ.pop("AKSPROFILE_STORE", None)
        try:
            with self.assertRaises(ValueError) as ctx:
                open_store()
            self.assertIn("AKSPROFILE_STORE", str(ctx.exception))
        finally:
            if saved is not None:
                _os.environ["AKSPROFILE_STORE"] = saved

    def test_invalid_profile_cannot_be_stored(self):
        bad = example_profile()
        bad["windows"][0]["end"] = "05:00"   # end before start
        with MemoryStore() as store:
            with self.assertRaises(ProfileError):
                store.save(bad)


# --------------------------------------------------------------- AC4 --------

class AC4_WorkerNeedsNoModel(unittest.TestCase):
    """The worker applies the right value with no model credentials configured,
    because no code path from the worker can reach a model (F8)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bounds = os.path.join(self.dir, "targets.json")
        with open(self.bounds, "w") as handle:
            json.dump({"rg/cluster1/apps": {"absoluteMin": 1, "absoluteMax": 10}}, handle)
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("OPENAI_API_KEY", "AKSPROFILE_API_KEY_VAR")}
        self._observe, self._apply = executor.observe, executor.apply
        self.applied = []
        executor.observe = lambda target: {"count": 1, "min": None, "max": None,
                                           "autoscale": False, "mode": "User",
                                           "state": "Succeeded"}

        def fake_apply(target, value, mode, dry_run=False, observed=None):
            self.applied.append((value, mode))
            return {"changed": True, "before": observed, "after": None,
                    "skipped": None, "command": "fake"}
        executor.apply = fake_apply
        worker.observe, worker.apply = executor.observe, executor.apply

    def tearDown(self):
        executor.observe, executor.apply = self._observe, self._apply
        worker.observe, worker.apply = self._observe, self._apply
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_no_model_credentials_are_needed(self):
        self.assertIsNone(os.environ.get("OPENAI_API_KEY"))
        with MemoryStore() as store:
            store.save(example_profile())
            records = worker.tick(store, now=at("2026-09-14 12:00"),
                                  bounds_path=self.bounds)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["action"], "applied")
        self.assertEqual(records[0]["desired"], 4)
        self.assertEqual(self.applied, [(4, "count")])

    def test_off_hours_applies_the_scale_down_value(self):
        with MemoryStore() as store:
            store.save(example_profile())
            worker.tick(store, now=at("2026-09-14 21:00"), bounds_path=self.bounds)
        self.assertEqual(self.applied, [(1, "count")])

    def test_paused_profile_is_not_applied(self):
        with MemoryStore() as store:
            store.save(example_profile())
            store.set_paused("apps-business-hours", True)
            records = worker.tick(store, now=at("2026-09-14 12:00"),
                                  bounds_path=self.bounds)
        self.assertEqual(records[0]["action"], "inactive")
        self.assertEqual(self.applied, [])

    def test_worker_module_cannot_reach_the_interpreter(self):
        """Check the import graph, not the prose -- the docstring legitimately
        mentions the module it refuses to import."""
        import ast
        import pathlib
        seen, queue = set(), ["worker"]
        pkg = pathlib.Path(worker.__file__).parent
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            path = pkg / (name + ".py")
            if not path.exists():
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.level:      # from .x import
                    queue.append(node.module or "")
                    for alias in node.names:
                        queue.append(alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("aksprofile."):
                            queue.append(alias.name.split(".", 1)[1])
        self.assertNotIn("interpret", seen,
                         "worker reaches interpret transitively via %s" % sorted(seen))


# --------------------------------------------------------------- AC5 --------

class AC5_CountVersusMinimum(unittest.TestCase):
    """'minimum four' -> floor; 'four nodes' -> exact count (F19)."""

    def _profile(self, mode):
        p = example_profile()
        p["mode"] = mode
        return p

    def test_modes_validate(self):
        self.assertIsNotNone(validate(self._profile("count")))
        self.assertIsNotNone(validate(self._profile("minimum")))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ProfileError):
            validate(self._profile("floor"))

    def test_mode_is_carried_into_the_decision(self):
        self.assertEqual(resolve(self._profile("minimum"), at("2026-09-14 12:00"))["mode"],
                         "minimum")

    def test_minimum_on_a_non_autoscaled_pool_is_skipped_not_forced(self):
        observed = {"count": 1, "min": None, "max": None, "autoscale": False,
                    "mode": "User", "state": "Succeeded"}
        result = executor.apply(example_profile()["target"], 4, "minimum",
                                observed=observed)
        self.assertFalse(result["changed"])
        self.assertIn("autoscaler", result["skipped"])

    def test_exact_count_on_an_autoscaled_pool_is_skipped_not_forced(self):
        observed = {"count": 1, "min": 1, "max": 5, "autoscale": True,
                    "mode": "User", "state": "Succeeded"}
        result = executor.apply(example_profile()["target"], 4, "count",
                                observed=observed)
        self.assertFalse(result["changed"])
        self.assertIn("autoscaler is enabled", result["skipped"])

    def test_mid_operation_pool_is_skipped(self):
        observed = {"count": 1, "autoscale": False, "mode": "User", "state": "Updating"}
        result = executor.apply(example_profile()["target"], 4, "count", observed=observed)
        self.assertFalse(result["changed"])
        self.assertIn("Updating", result["skipped"])


# --------------------------------------------------------------- AC6 --------

class AC6_Clamping(unittest.TestCase):
    BOUNDS = {"absoluteMin": 1, "absoluteMax": 5}

    def test_value_above_max_is_clamped_and_reported(self):
        value, was_clamped, note = clamp(400, self.BOUNDS)
        self.assertEqual(value, 5)
        self.assertTrue(was_clamped)
        self.assertIn("400", note)

    def test_value_below_min_is_clamped(self):
        value, was_clamped, _ = clamp(0, self.BOUNDS)
        self.assertEqual(value, 1)
        self.assertTrue(was_clamped)

    def test_in_range_is_untouched(self):
        value, was_clamped, note = clamp(3, self.BOUNDS)
        self.assertEqual((value, was_clamped, note), (3, False, None))

    def test_system_pool_never_goes_to_zero(self):
        value, was_clamped, note = clamp(0, {"absoluteMin": 0, "absoluteMax": 5},
                                         is_system_pool=True)
        self.assertEqual(value, 1)
        self.assertIn("system pool", note)

    def test_missing_absolute_max_is_an_error(self):
        with self.assertRaises(ProfileError):
            clamp(3, {"absoluteMin": 1})

    def test_unconfigured_target_gets_the_tight_fallback(self):
        got, defaulted = bounds_mod.for_target(example_profile()["target"], table={})
        self.assertTrue(defaulted)
        self.assertEqual(got["absoluteMax"], bounds_mod.FALLBACK["absoluteMax"])


# --------------------------------------------------------------- AC7 --------

class AC7_UnknownTargetRejected(unittest.TestCase):
    def setUp(self):
        from aksprofile import discovery
        self.discovery = discovery
        self._clusters = discovery.Discovery.clusters
        self._pools = discovery.Discovery.node_pools
        discovery.Discovery.clusters = lambda self: [
            {"name": "cluster1", "resourceGroup": "rg", "subscription": "sub",
             "location": "eastus", "powerState": "Running"}]
        discovery.Discovery.node_pools = lambda self, c, g: [
            {"name": "apps", "mode": "User", "count": 1, "min": None,
             "max": None, "autoscale": False, "vmSize": "Standard_B4ms"}]

    def tearDown(self):
        self.discovery.Discovery.clusters = self._clusters
        self.discovery.Discovery.node_pools = self._pools

    def test_known_target_resolves(self):
        found = self.discovery.Discovery().resolve_target("cluster1", "apps")
        self.assertEqual(found["target"]["nodePool"], "apps")
        self.assertEqual(found["target"]["resourceGroup"], "rg")

    def test_matching_is_case_insensitive(self):
        found = self.discovery.Discovery().resolve_target("Cluster1", "APPS")
        self.assertEqual(found["target"]["cluster"], "cluster1")

    def test_unknown_pool_is_rejected_and_lists_what_exists(self):
        with self.assertRaises(self.discovery.DiscoveryError) as ctx:
            self.discovery.Discovery().resolve_target("cluster1", "batch")
        self.assertIn("apps", str(ctx.exception))

    def test_unknown_cluster_is_rejected(self):
        with self.assertRaises(self.discovery.DiscoveryError) as ctx:
            self.discovery.Discovery().resolve_target("nope", "apps")
        self.assertIn("cluster1", str(ctx.exception))


# --------------------------------------------------------------- AC8 --------

class AC8_DaylightSaving(unittest.TestCase):
    """A weekday window resolves correctly on both sides of a DST transition,
    and across the gap and the repeat (DESIGN.md section 5)."""

    def test_wall_clock_holds_across_spring_forward(self):
        p = example_profile()
        # 2026-03-08 is spring-forward in America/Los_Angeles.
        self.assertEqual(resolve(p, at("2026-03-06 06:00"))["value"], 4)  # Fri before
        self.assertEqual(resolve(p, at("2026-03-09 06:00"))["value"], 4)  # Mon after
        self.assertEqual(resolve(p, at("2026-03-06 05:59"))["value"], 1)
        self.assertEqual(resolve(p, at("2026-03-09 05:59"))["value"], 1)

    def test_window_opening_in_the_skipped_hour_takes_effect_after_the_gap(self):
        p = example_profile()
        p["windows"] = [{"days": ["Sun"], "start": "02:30", "end": "10:00", "value": 4}]
        before = _dt.datetime(2026, 3, 8, 9, 30, tzinfo=_dt.timezone.utc)  # 01:30 PST
        after = _dt.datetime(2026, 3, 8, 10, 0, tzinfo=_dt.timezone.utc)   # 03:00 PDT
        self.assertEqual(before.astimezone(LA).strftime("%H:%M"), "01:30")
        self.assertEqual(after.astimezone(LA).strftime("%H:%M"), "03:00")
        self.assertEqual(resolve(p, before)["value"], 1)
        self.assertEqual(resolve(p, after)["value"], 4)

    def test_window_spanning_the_repeated_hour_does_not_toggle(self):
        p = example_profile()
        p["windows"] = [{"days": ["Sun"], "start": "01:30", "end": "10:00", "value": 4}]
        # 2026-11-01: 01:00-01:59 happens twice in America/Los_Angeles.
        first = _dt.datetime(2026, 11, 1, 8, 30, tzinfo=_dt.timezone.utc)   # 01:30 PDT
        second = _dt.datetime(2026, 11, 1, 9, 30, tzinfo=_dt.timezone.utc)  # 01:30 PST
        self.assertEqual(first.astimezone(LA).strftime("%H:%M %Z"), "01:30 PDT")
        self.assertEqual(second.astimezone(LA).strftime("%H:%M %Z"), "01:30 PST")
        self.assertEqual(resolve(p, first)["value"], 4)
        self.assertEqual(resolve(p, second)["value"], 4)

    def test_caller_timezone_does_not_matter(self):
        p = example_profile()
        instant = _dt.datetime(2026, 9, 14, 19, 0, tzinfo=_dt.timezone.utc)  # 12:00 LA
        for tz in ("UTC", "Asia/Tokyo", "Europe/London"):
            with self.subTest(tz=tz):
                self.assertEqual(resolve(p, instant.astimezone(ZoneInfo(tz)))["value"], 4)


if __name__ == "__main__":
    unittest.main()


class ConflictResolution(unittest.TestCase):
    """DESIGN.md section 7. Before this, the worker applied every profile, so two
    actives on one node pool undid each other every tick -- which moved a live
    pool 1 -> 2 -> 1 mid-Scaling."""

    def _profile(self, name, updated, pool="apps", value=4):
        p = example_profile()
        p["name"] = name
        p["updatedAt"] = updated
        p["target"] = dict(p["target"], nodePool=pool)
        p["windows"][0]["value"] = value
        return p

    def test_one_winner_per_target(self):
        winners, suppressed = worker.select_winners([
            self._profile("older", "2026-09-09T10:00:00+00:00"),
            self._profile("newer", "2026-09-09T18:00:00+00:00"),
        ])
        self.assertEqual([w["name"] for w in winners], ["newer"])
        self.assertEqual([(l["name"], w) for l, w in suppressed], [("older", "newer")])

    def test_winner_is_independent_of_list_order(self):
        """store.list() ordering is a backend detail -- TableStore sorts by
        updated_at, MemoryStore does not."""
        a = self._profile("older", "2026-09-09T10:00:00+00:00")
        b = self._profile("newer", "2026-09-09T18:00:00+00:00")
        for order in ([a, b], [b, a]):
            with self.subTest(order=[p["name"] for p in order]):
                winners, _ = worker.select_winners(order)
                self.assertEqual([w["name"] for w in winners], ["newer"])

    def test_different_targets_both_apply(self):
        """A fix that over-grouped would quietly break the normal case."""
        winners, suppressed = worker.select_winners([
            self._profile("apps", "2026-09-09T10:00:00+00:00", pool="apps"),
            self._profile("batch", "2026-09-09T11:00:00+00:00", pool="batch"),
        ])
        self.assertEqual(sorted(w["name"] for w in winners), ["apps", "batch"])
        self.assertEqual(suppressed, [])

    def test_paused_profiles_never_suppress_an_active_one(self):
        paused = self._profile("paused-but-newer", "2026-09-09T23:00:00+00:00")
        paused["paused"] = True
        active = self._profile("active", "2026-09-09T10:00:00+00:00")
        winners, suppressed = worker.select_winners([paused, active])
        self.assertEqual([w["name"] for w in winners], ["active"])
        self.assertEqual(suppressed, [])

    def test_ties_are_deterministic(self):
        same = "2026-09-09T12:00:00+00:00"
        first, _ = worker.select_winners([self._profile("aaa", same),
                                          self._profile("bbb", same)])
        second, _ = worker.select_winners([self._profile("bbb", same),
                                           self._profile("aaa", same)])
        self.assertEqual([w["name"] for w in first], [w["name"] for w in second])

    def test_a_profile_with_no_updatedAt_loses_to_one_that_has_it(self):
        old = self._profile("legacy", "2026-09-09T12:00:00+00:00")
        del old["updatedAt"]
        winners, _ = worker.select_winners([old, self._profile("current",
                                                               "2026-09-09T09:00:00+00:00")])
        self.assertEqual([w["name"] for w in winners], ["current"])

    def test_save_stamps_updatedAt(self):
        store = MemoryStore()
        saved = store.save(example_profile())
        self.assertIn("updatedAt", saved)
        self.assertIn("updatedAt", store.get(saved["name"]))
