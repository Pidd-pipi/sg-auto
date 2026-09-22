"""Tests for the Solo Manager blocklist and candidate filtering."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from api.common import DEFAULT_CONFIG
from api.platform import PlatformProvider


class BlocklistTests(unittest.TestCase):
    def _provider(self):
        return PlatformProvider(DEFAULT_CONFIG)

    def test_set_blocklist_normalises_case(self):
        provider = self._provider()
        provider.set_blocklist([" GB-1 ", "gb-2", ""])
        self.assertEqual(provider.blocked_codes(), {"gb-1", "gb-2"})

    def test_is_blocked_is_case_insensitive(self):
        provider = self._provider()
        provider.set_blocklist(["GB-1"])
        self.assertTrue(provider.is_blocked("gb-1"))
        self.assertFalse(provider.is_blocked("gb-2"))

    def test_empty_blocklist_blocks_nothing(self):
        provider = self._provider()
        provider.set_blocklist([])
        self.assertEqual(provider.blocked_codes(), set())
        self.assertFalse(provider.is_blocked("gb-1"))


class QuotaMovementTests(unittest.TestCase):
    """The ledger calls the verified release endpoint."""

    def _provider(self, calls):
        provider = PlatformProvider(DEFAULT_CONFIG)

        def fake_request(base_url, path, *, token="", method="GET", payload=None, timeout=5):
            calls.append((method, path))
            if path == "/auth/me":
                return {"id": "u"}
            if path == "/tasks":
                return {"id": "task-9", "taskNo": "gb-9-代码生成-1", "rounds": [{"id": "r1"}]}
            return {}

        provider._resolve_manager_connection = lambda pb: ("http://manager", "tok", "")
        import api.platform as platform_module
        original = platform_module.manager_request_json
        platform_module.manager_request_json = fake_request
        self.addCleanup(setattr, platform_module, "manager_request_json", original)
        return provider

    def test_pre_deduct_posts_to_tasks(self):
        calls = []
        provider = self._provider(calls)
        result = provider.pre_deduct("variant-1", "0-1代码生成")
        self.assertEqual(result["platformTaskId"], "task-9")
        self.assertEqual(result["platformTaskNo"], "gb-9-代码生成-1")
        self.assertIn(("POST", "/tasks"), calls)

    def test_release_task_posts_to_cancel(self):
        calls = []
        provider = self._provider(calls)
        outcome = provider.release_task("task-9")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["mode"], "platform")
        self.assertIn(("POST", "/tasks/task-9/cancel"), calls)

    def test_release_without_task_id_is_local_only(self):
        provider = self._provider([])
        outcome = provider.release_task("")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["mode"], "local")


if __name__ == "__main__":
    unittest.main()
