"""
Store layer: URL parsing, backend selection, and the concurrency guarantee.

Runs entirely offline. The Azure backend is exercised only where it can be
without the SDK -- selection and error messages -- so this suite never needs a
network or a credential.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aksprofile.model import ProfileError                       # noqa: E402
from aksprofile.store import (                                  # noqa: E402
    ConcurrentModification, MemoryStore, StoreBackend, TableStore, open_store,
    parse_url,
)
from test_acceptance import example_profile                     # noqa: E402


class TestUrlParsing(unittest.TestCase):
    def test_table(self):
        self.assertEqual(parse_url("table://mystorageacct"), ("table", "mystorageacct"))

    def test_table_needs_an_account(self):
        with self.assertRaises(ValueError):
            parse_url("table://")

    def test_memory_is_available_for_tests(self):
        self.assertEqual(parse_url("memory://"), ("memory", ""))

    def test_unknown_scheme_is_rejected(self):
        for url in ("sqlite:///tmp/x.db", "postgres://host/db", "s3://bucket", "mystore"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    parse_url(url)


class TestBackendSelection(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in ("AKSPROFILE_STORE",)}
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_explicit_url_wins(self):
        os.environ["AKSPROFILE_STORE"] = "table://ignored"
        with open_store("memory://") as store:
            self.assertIsInstance(store, MemoryStore)

    def test_env_store_is_used(self):
        os.environ["AKSPROFILE_STORE"] = "memory://"
        with open_store() as store:
            self.assertIsInstance(store, MemoryStore)

    def test_no_store_configured_is_an_error_not_a_fallback(self):
        with self.assertRaises(ValueError) as ctx:
            open_store()
        self.assertIn("AKSPROFILE_STORE", str(ctx.exception))

    def test_the_deployable_backend_is_the_durable_one(self):
        self.assertTrue(TableStore.durable)
        self.assertFalse(MemoryStore.durable)

    def test_table_backend_explains_its_missing_dependency(self):
        """Selecting the cloud backend without the SDK must say what to install,
        not raise ImportError from somewhere in the stack."""
        try:
            import azure.data.tables  # noqa: F401
            self.skipTest("azure-data-tables is installed")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as ctx:
            open_store("table://someaccount")
        self.assertIn("pip install", str(ctx.exception))


class TestBackendContract(unittest.TestCase):
    """Everything every backend must do. Run against the memory backend here;
    the same assertions apply to TableStore against a real account."""

    def setUp(self):
        self.store = MemoryStore()

    def tearDown(self):
        self.store.close()

    def test_backend_implements_the_interface(self):
        for method in ("save", "get", "get_with_etag", "list", "delete",
                       "set_paused", "conflicting", "record_run", "recent_runs"):
            self.assertTrue(callable(getattr(self.store, method)), method)

    def test_save_get_roundtrip(self):
        self.store.save(example_profile())
        got = self.store.get("apps-business-hours")
        self.assertEqual(got["windows"][0]["value"], 4)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("nope"))
        self.assertEqual(self.store.get_with_etag("nope"), (None, None))

    def test_created_at_is_preserved_across_updates(self):
        saved = self.store.save(example_profile())
        first = saved["createdAt"]
        updated = dict(example_profile())
        updated["otherwise"] = 2
        again = self.store.save(updated)
        self.assertEqual(again["createdAt"], first)

    def test_delete(self):
        self.store.save(example_profile())
        self.assertTrue(self.store.delete("apps-business-hours"))
        self.assertFalse(self.store.delete("apps-business-hours"))

    def test_invalid_profile_is_rejected(self):
        bad = example_profile()
        bad["mode"] = "sideways"
        with self.assertRaises(ProfileError):
            self.store.save(bad)

    def test_conflicting_finds_same_target(self):
        self.store.save(example_profile())
        other = example_profile()
        other["name"] = "second"
        self.assertEqual(self.store.conflicting(other), ["apps-business-hours"])

    def test_conflicting_ignores_paused_and_other_targets(self):
        first = example_profile()
        self.store.save(first)
        self.store.set_paused("apps-business-hours", True)
        other = example_profile()
        other["name"] = "second"
        self.assertEqual(self.store.conflicting(other), [])

        elsewhere = example_profile()
        elsewhere["name"] = "third"
        elsewhere["target"] = dict(elsewhere["target"], nodePool="batch")
        self.store.save(elsewhere)
        self.assertEqual(self.store.conflicting(elsewhere), [])

    def test_runs_are_recorded(self):
        self.store.save(example_profile())
        self.store.record_run("apps-business-hours", "applied", "set to 4")
        recent = self.store.recent_runs()
        self.assertEqual(recent[0]["action"], "applied")


class TestOptimisticConcurrency(unittest.TestCase):
    """A lost update becomes an error instead of silently clobbering."""

    def setUp(self):
        self.store = MemoryStore()
        self.store.save(example_profile())

    def tearDown(self):
        self.store.close()

    def test_stale_etag_is_rejected(self):
        _, etag = self.store.get_with_etag("apps-business-hours")
        # someone else writes first
        other = example_profile()
        other["otherwise"] = 2
        self.store.save(other)
        # our write, carrying the etag we read, must fail
        mine = example_profile()
        mine["otherwise"] = 3
        with self.assertRaises(ConcurrentModification):
            self.store.save(mine, etag=etag)
        self.assertEqual(self.store.get("apps-business-hours")["otherwise"], 2)

    def test_current_etag_succeeds(self):
        profile, etag = self.store.get_with_etag("apps-business-hours")
        profile["otherwise"] = 7
        self.store.save(profile, etag=etag)
        self.assertEqual(self.store.get("apps-business-hours")["otherwise"], 7)

    def test_etag_advances_on_each_write(self):
        _, first = self.store.get_with_etag("apps-business-hours")
        self.store.save(example_profile())
        _, second = self.store.get_with_etag("apps-business-hours")
        self.assertNotEqual(first, second)

    def test_set_paused_uses_the_etag_it_read(self):
        self.store.set_paused("apps-business-hours", True)
        self.assertTrue(self.store.get("apps-business-hours")["paused"])


if __name__ == "__main__":
    unittest.main()
