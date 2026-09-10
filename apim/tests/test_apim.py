"""
Acceptance tests, mapped to DESIGN.md section 9.

Offline: no Azure, no model, no network.
"""

import datetime as _dt
import os
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apimprofile import executor, worker                      # noqa: E402
from apimprofile.model import (                               # noqa: E402
    ProfileError, check_tier_schedulable, check_units_for_tier,
    check_units_for_zones, clamp, describe, expected_scale_seconds,
    min_window_minutes, resolve, validate,
)
from apimprofile.store import MemoryStore, TableStore, open_store, parse_url  # noqa: E402

CHI = ZoneInfo("America/Chicago")


def profile(**over):
    p = {
        "name": "apim-business-hours",
        "target": {"subscription": "sub", "resourceGroup": "rg", "service": "my-apim"},
        "timezone": "America/Chicago",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "scaleUpAt": "09:00",
        "scaleDownAt": "18:00",
        "units": 4,
        "baselineUnits": 2,
        "prewarmMinutes": 45,
        "endDate": None,
        "paused": False,
        "sourceText": "Scale my-apim to 4 units at 9am Central and back down at 6pm.",
    }
    p.update(over)
    return p


def at(local, tz=CHI):
    return _dt.datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=tz)


def observed(capacity=2, tier="Premium", state="Succeeded", target_state="", zones=0):
    return {"capacity": capacity, "tier": tier, "zoneCount": zones,
            "provisioningState": state, "targetProvisioningState": target_state,
            "inFlight": bool(target_state)}


# ------------------------------------------------------------- AC1 / AC8 ----

class AC1_TheWorkedExample(unittest.TestCase):
    def test_validates(self):
        self.assertIsNotNone(validate(profile()))

    def test_resolution_including_prewarm(self):
        p = profile()
        cases = [
            ("2026-09-14 08:14", 2),   # Mon, before pre-warm
            ("2026-09-14 08:15", 4),   # pre-warm start: 09:00 - 45m
            ("2026-09-14 09:00", 4),   # the time the user asked for
            ("2026-09-14 17:59", 4),
            ("2026-09-14 18:00", 2),   # back to baseline
            ("2026-09-19 12:00", 2),   # Saturday
        ]
        for local, expected in cases:
            with self.subTest(local=local):
                self.assertEqual(resolve(p, at(local))["units"], expected)

    def test_report_shows_the_issue_time_not_just_the_business_time(self):
        """AC8 -- prewarm shifts the effective start, but the stored scaleUpAt
        still records what the user asked for."""
        text = describe(profile(), tier="Premium")
        self.assertIn("09:00", text)          # what they asked for
        self.assertIn("issued at 08:15", text)  # when it actually fires
        self.assertEqual(profile()["scaleUpAt"], "09:00")


# ------------------------------------------------------------------ AC3 -----

class AC3_UnschedulableTiers(unittest.TestCase):
    def test_developer_is_refused_with_a_reason(self):
        with self.assertRaises(ProfileError) as ctx:
            check_tier_schedulable("Developer")
        self.assertIn("cannot add units", str(ctx.exception))

    def test_consumption_is_refused_with_a_reason(self):
        with self.assertRaises(ProfileError) as ctx:
            check_tier_schedulable("Consumption")
        self.assertIn("scales itself", str(ctx.exception))

    def test_schedulable_tiers_pass(self):
        for tier in ("Basic", "Standard", "Premium", "StandardV2", "Premium v2"):
            with self.subTest(tier=tier):
                self.assertTrue(check_tier_schedulable(tier))

    def test_unknown_tier_is_refused(self):
        with self.assertRaises(ProfileError):
            check_tier_schedulable("Enormous")


# ------------------------------------------------------------------ AC4 -----

class AC4_TierMaximums(unittest.TestCase):
    CASES = [("Standard", 4, True), ("Standard", 5, False),
             ("StandardV2", 10, True), ("StandardV2", 11, False),
             ("PremiumV2", 30, True), ("PremiumV2", 31, False),
             ("Premium", 50, True)]   # no documented fixed limit

    def test_tier_limits(self):
        for tier, units, ok in self.CASES:
            with self.subTest(tier=tier, units=units):
                if ok:
                    self.assertEqual(check_units_for_tier(units, tier), units)
                else:
                    with self.assertRaises(ProfileError):
                        check_units_for_tier(units, tier)

    def test_tier_max_beats_a_generous_bound(self):
        """A configured bound cannot exceed what the tier allows."""
        units, was_clamped, note = clamp(9, {"absoluteMin": 1, "absoluteMax": 20},
                                         tier="Standard")
        self.assertEqual(units, 4)
        self.assertTrue(was_clamped)
        self.assertIn("Standard tier max 4", note)

    def test_configured_bound_clamps_below_tier_max(self):
        units, was_clamped, _ = clamp(8, {"absoluteMin": 1, "absoluteMax": 3},
                                      tier="Premium")
        self.assertEqual(units, 3)
        self.assertTrue(was_clamped)


# ------------------------------------------------------------------ AC5 -----

class AC5_AvailabilityZones(unittest.TestCase):
    def test_non_multiple_is_refused(self):
        with self.assertRaises(ProfileError) as ctx:
            check_units_for_zones(5, 3)
        self.assertIn("multiple of the 3", str(ctx.exception))

    def test_multiple_is_accepted(self):
        self.assertEqual(check_units_for_zones(6, 3), 6)

    def test_no_zones_means_no_constraint(self):
        self.assertEqual(check_units_for_zones(5, 0), 5)
        self.assertEqual(check_units_for_zones(5, 1), 5)

    def test_clamp_rounds_down_to_a_multiple(self):
        """A clamp must never hand back MORE than was asked for."""
        units, was_clamped, note = clamp(7, {"absoluteMin": 1, "absoluteMax": 30},
                                         tier="Premium", zone_count=3)
        self.assertEqual(units, 6)
        self.assertTrue(was_clamped)
        self.assertIn("3 zones", note)


# ------------------------------------------------------------------ AC6 -----

class AC6_WindowMustFitAScale(unittest.TestCase):
    def test_short_window_is_refused(self):
        with self.assertRaises(ProfileError) as ctx:
            validate(profile(scaleUpAt="09:00", scaleDownAt="09:30"))
        self.assertIn("at least", str(ctx.exception))

    def test_ninety_minutes_is_the_default_minimum(self):
        self.assertEqual(min_window_minutes(), 90)

    def test_a_long_enough_window_passes(self):
        self.assertIsNotNone(validate(profile(scaleUpAt="09:00", scaleDownAt="10:30")))

    def test_measured_duration_lowers_the_bar(self):
        """Once a real duration is known, a shorter window can be legitimate."""
        p = profile(scaleUpAt="09:00", scaleDownAt="10:00", expectedScaleSeconds=20 * 60)
        self.assertIsNotNone(validate(p))

    def test_overnight_window_is_refused(self):
        with self.assertRaises(ProfileError):
            validate(profile(scaleUpAt="18:00", scaleDownAt="09:00"))


# ------------------------------------------------------------------ AC7 -----

class AC7_InFlightIsNotAnError(unittest.TestCase):
    """APIM locks while it changes. Issuing into that produces a failure, so the
    worker must recognise it and stay quiet (DESIGN.md 3.2)."""

    def test_in_flight_is_skipped_not_written(self):
        result = executor.apply({"subscription": "s", "resourceGroup": "rg",
                                 "service": "a"}, 4,
                                observed=observed(target_state="Updating"))
        self.assertFalse(result["changed"])
        self.assertIn("in flight", result["skipped"])
        self.assertIsNone(result["command"])

    def test_mid_operation_provisioning_state_is_skipped(self):
        result = executor.apply({"subscription": "s", "resourceGroup": "rg",
                                 "service": "a"}, 4,
                                observed=observed(state="Updating"))
        self.assertFalse(result["changed"])
        self.assertIn("Updating", result["skipped"])

    def test_already_correct_is_nochange(self):
        result = executor.apply({"subscription": "s", "resourceGroup": "rg",
                                 "service": "a"}, 2, observed=observed(capacity=2))
        self.assertFalse(result["changed"])
        self.assertIsNone(result["skipped"])

    def test_dry_run_builds_the_command_but_does_not_run_it(self):
        result = executor.apply({"subscription": "s", "resourceGroup": "rg",
                                 "service": "a"}, 4, dry_run=True,
                                observed=observed(capacity=2))
        self.assertEqual(result["skipped"], "dry run")
        self.assertIn("patch", result["command"])


# ------------------------------------------------------------- AC9 / AC10 ---

class AC9_WorkerNeedsNoModel(unittest.TestCase):
    def test_worker_cannot_reach_the_interpreter(self):
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
                if isinstance(node, ast.ImportFrom) and node.level:
                    queue.append(node.module or "")
                    for alias in node.names:
                        queue.append(alias.name)
        self.assertNotIn("interpret", seen,
                         "worker reaches interpret via %s" % sorted(seen))


class AC10_TierIsNeverChanged(unittest.TestCase):
    """sku.name must never be written by a schedule (DESIGN.md 7)."""

    def test_the_patch_body_carries_the_current_tier_unchanged(self):
        result = executor.apply({"subscription": "s", "resourceGroup": "rg",
                                 "service": "a"}, 4, dry_run=True,
                                observed=observed(capacity=2, tier="Premium"))
        self.assertIn('"name": "Premium"', result["command"])
        self.assertIn('"capacity": 4', result["command"])

    def test_refuses_to_patch_without_knowing_the_tier(self):
        with self.assertRaises(executor.ExecutionError):
            executor.apply({"subscription": "s", "resourceGroup": "rg", "service": "a"},
                           4, observed=observed(capacity=2, tier=None))

    def test_no_source_file_assigns_a_tier(self):
        import pathlib
        pkg = pathlib.Path(worker.__file__).parent
        for path in pkg.glob("*.py"):
            text = path.read_text()
            self.assertNotIn('"name": "Premium"', text.replace(
                'json.dumps({"sku": {"name": tier', ''), path.name)


# --------------------------------------------------------- duration (D1) ----

class D1_ScaleDurationIsMeasured(unittest.TestCase):
    """The scheduler measures its own scale time rather than being told."""

    def setUp(self):
        self.store = MemoryStore()
        self.now = _dt.datetime(2026, 9, 14, 14, 0, tzinfo=_dt.timezone.utc)

    def test_pending_is_recorded_then_settled(self):
        self.store.set_state("p", {"pending": {
            "issuedAt": (self.now - _dt.timedelta(minutes=27)).isoformat(),
            "from": 2, "to": 4}})
        settled = worker._settle_pending(self.store, "p", observed(capacity=4), self.now)
        self.assertEqual(settled["seconds"], 27 * 60)
        self.assertEqual(self.store.get_state("p"), {})

    def test_still_in_flight_is_not_settled(self):
        self.store.set_state("p", {"pending": {
            "issuedAt": self.now.isoformat(), "from": 2, "to": 4}})
        self.assertIsNone(worker._settle_pending(
            self.store, "p", observed(capacity=2, target_state="Updating"), self.now))
        self.assertIn("pending", self.store.get_state("p"))

    def test_landing_somewhere_else_discards_the_measurement(self):
        """Someone scaled it by hand; a duration recorded now would be a lie."""
        self.store.set_state("p", {"pending": {
            "issuedAt": self.now.isoformat(), "from": 2, "to": 4}})
        self.assertIsNone(worker._settle_pending(
            self.store, "p", observed(capacity=7), self.now))
        self.assertEqual(self.store.get_state("p"), {})

    def test_durations_surface_for_reporting(self):
        self.store.record_run("p", "issued", "", duration=1620)
        self.store.record_run("p", "issued", "", duration=1380)
        self.assertEqual(sorted(self.store.scale_durations()), [1380, 1620])


# ------------------------------------------------------------------ store ---

class StoreContract(unittest.TestCase):
    def test_only_durable_backend_is_deployable(self):
        self.assertFalse(MemoryStore.durable)
        self.assertTrue(TableStore.durable)

    def test_no_silent_default_store(self):
        """Isolate from any .env in the working directory -- a test that passes
        or fails depending on whether someone has configured their laptop is
        worse than no test."""
        from apimprofile import config
        saved_env = os.environ.pop("APIMPROFILE_STORE", None)
        saved_file = os.environ.get("APIMPROFILE_ENV_FILE")
        saved_loaded = config._LOADED
        os.environ["APIMPROFILE_ENV_FILE"] = "/nonexistent/.env"
        config._LOADED = False
        try:
            config.load_dotenv()
            with self.assertRaises(ValueError) as ctx:
                open_store()
            self.assertIn("APIMPROFILE_STORE", str(ctx.exception))
        finally:
            config._LOADED = saved_loaded
            if saved_env is not None:
                os.environ["APIMPROFILE_STORE"] = saved_env
            if saved_file is None:
                os.environ.pop("APIMPROFILE_ENV_FILE", None)
            else:
                os.environ["APIMPROFILE_ENV_FILE"] = saved_file

    def test_url_parsing(self):
        self.assertEqual(parse_url("table://acct"), ("table", "acct"))
        self.assertEqual(parse_url("memory://"), ("memory", ""))
        for bad in ("sqlite:///x.db", "postgres://h/d", "nope"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    parse_url(bad)

    def test_roundtrip_and_conflict_detection(self):
        store = MemoryStore()
        store.save(profile())
        other = profile(name="second")
        self.assertEqual(store.conflicting(other), ["apim-business-hours"])
        store.set_paused("apim-business-hours", True)
        self.assertEqual(store.conflicting(other), [])


if __name__ == "__main__":
    unittest.main()


class Regressions(unittest.TestCase):
    """Bugs found by probing the built app. Each one is here so it stays dead."""

    BOUNDS = {"absoluteMin": 1, "absoluteMax": 30}

    def test_zone_rounding_never_increases_the_request(self):
        """A guardrail that hands back MORE than was asked for is a cost bug.
        Rounding up to the nearest multiple turned a request for 1 unit into 3."""
        for units in (1, 2, 4, 5, 7, 8):
            with self.subTest(units=units):
                got, _, _ = clamp(units, self.BOUNDS, tier="Premium", zone_count=3)
                self.assertLessEqual(got, units,
                                     "clamp raised %d to %d" % (units, got))

    def test_zone_rounding_never_breaches_absolute_max(self):
        """bounds 1..2 on a 3-zone instance has no valid multiple. Rounding up
        would have returned 3 -- above absoluteMax, defeating the whole point."""
        got, was_clamped, note = clamp(2, {"absoluteMin": 1, "absoluteMax": 2},
                                       tier="Premium", zone_count=3)
        self.assertLessEqual(got, 2)
        self.assertTrue(was_clamped)
        self.assertIn("no multiple of 3 fits", note)

    def test_zone_rounding_still_works_when_a_multiple_exists(self):
        got, was_clamped, _ = clamp(7, self.BOUNDS, tier="Premium", zone_count=3)
        self.assertEqual(got, 6)
        self.assertTrue(was_clamped)

    def test_zone_error_does_not_suggest_the_same_number_twice(self):
        """Requests below the zone count produced 'use 3 or 3'."""
        for units in (1, 2):
            with self.subTest(units=units):
                with self.assertRaises(ProfileError) as ctx:
                    check_units_for_zones(units, 3)
                message = str(ctx.exception)
                self.assertIn("use 3", message)
                self.assertNotIn("3 or 3", message)

    def test_zone_error_offers_both_options_when_both_are_valid(self):
        with self.assertRaises(ProfileError) as ctx:
            check_units_for_zones(5, 3)
        self.assertIn("3 or 6", str(ctx.exception))

    def test_minimum_window_is_tier_aware(self):
        """A flat 90-minute floor came from the docs' 15-45 minutes, which
        describes classic tiers. A BasicV2 instance scaled in under a minute, so
        that floor would refuse perfectly coherent v2 windows."""
        self.assertEqual(min_window_minutes(tier="BasicV2"), 10)
        self.assertEqual(min_window_minutes(tier="StandardV2"), 10)
        self.assertEqual(min_window_minutes(tier="PremiumV2"), 20)
        self.assertEqual(min_window_minutes(tier="Standard"), 90)
        self.assertEqual(min_window_minutes(), 90)      # unknown tier stays safe

    def test_a_measured_duration_beats_the_tier_default(self):
        self.assertEqual(min_window_minutes(scale_seconds=120, tier="Standard"), 4)

    def test_short_window_is_allowed_on_a_fast_tier(self):
        p = profile(scaleUpAt="16:20", scaleDownAt="16:50", tier="BasicV2")
        self.assertIsNotNone(validate(p))

    def test_the_same_window_is_still_refused_on_a_slow_tier(self):
        with self.assertRaises(ProfileError):
            validate(profile(scaleUpAt="16:20", scaleDownAt="16:50", tier="Standard"))
