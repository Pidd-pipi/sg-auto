"""End-to-end tests against a live server on a throwaway state directory."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from api.common import SettingsStore  # noqa: E402
from api.service import SchedulerService  # noqa: E402
from server import Handler, MonitorInstanceLock, MonitorHTTPServer  # noqa: E402
from tests.support import make_config, write_task  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "tasks").mkdir(parents=True, exist_ok=True)
        cls.config = make_config(cls.root)
        cls.config["automation"]["paused"] = True
        cls.service = SchedulerService(cls.config)
        cls.service.settings = SettingsStore(cls.root / ".state" / "settings.json")
        cls.port = free_port()
        cls.server = MonitorHTTPServer(("127.0.0.1", cls.port), Handler, cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        write_task(self.root, "gb-live-20260920", status="running")

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_reports_hub_stats(self):
        status, data = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("hub", data["stats"])

    def test_task_list_is_slim_and_detail_is_heavy(self):
        status, data = self._get("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 1)
        card = data["tasks"][0]
        self.assertNotIn("promptText", card)
        self.assertNotIn("sides", card)

        status, detail = self._get(f"/api/tasks/{card['id']}")
        self.assertEqual(status, 200)
        self.assertIn("sides", detail)
        self.assertIn("workflow", detail)
        self.assertEqual(detail["promptText"], "题目提示词")

    def test_task_log_reads_from_the_tail(self):
        card = self._get("/api/tasks")[1]["tasks"][0]
        status, data = self._get(f"/api/tasks/{card['id']}/log?side=A&lines=10")
        self.assertEqual(status, 200)
        self.assertIn("lines", data)

    def test_settings_round_trip_without_leaking_credentials(self):
        status, data = self._post("/api/settings", {"settings": {"ui": {"density": "compact"}}})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["settings"]["ui"]["density"], "compact")
        status, data = self._get("/api/settings")
        self.assertEqual(status, 200)
        # Only the saved/unsaved marker may come back, never the secret itself.
        self.assertEqual(data["settings"]["manager"]["passwordSaved"], False)
        self.assertNotIn("managerPassword", json.dumps(data["settings"]))

    def test_unknown_folder_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError):
            self._post("/api/settings", {"settings": {"defaultFolderId": "not-a-real-folder"}})

    def test_snapshot_payload_is_small(self):
        status, data = self._get("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertIn("tasks", data)
        self.assertIn("queue", data)
        self.assertIn("containers", data)
        self.assertEqual(data["queue"]["scheduleMode"], "containers")

    def test_task_events_are_structured(self):
        card = self._get("/api/tasks")[1]["tasks"][0]
        status, data = self._get(f"/api/tasks/{card['id']}/events?side=A&limit=50")
        self.assertEqual(status, 200)
        self.assertIn("events", data)
        self.assertIn("path", data)
        for event in data["events"]:
            self.assertIn("kind", event)
            self.assertIn("title", event)

    def test_logs_endpoint_returns_entries(self):
        status, data = self._get("/api/logs?limit=50")
        self.assertEqual(status, 200)
        self.assertTrue(any(entry["event"] == "service.started" for entry in data["entries"]))

    def test_automation_mode_switch(self):
        status, data = self._post("/api/automation", {"action": "set-schedule-mode", "mode": "tasks"})
        self.assertEqual(status, 200)
        self.assertEqual(data["automation"]["scheduleMode"], "tasks")
        self._post("/api/automation", {"action": "set-schedule-mode", "mode": "containers"})

    def test_parallel_resume_is_gone(self):
        """There is no "run both sides at once" action any more.

        Validated against a real task so the assertion proves the rejection came
        from the side/mode check rather than from a missing task.
        """
        card = self._get("/api/tasks")[1]["tasks"][0]
        self.assertIsNotNone(card)
        for side, mode, expected in (
            ("BOTH", "both", "side 只能为 A 或 B"),
            ("A", "both", "mode 只能为 resume 或 rerun"),
            ("both", "resume", "side 只能为 A 或 B"),
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/action", {"taskId": card["id"], "side": side, "mode": mode})
            self.assertEqual(ctx.exception.code, 400)
            body = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertEqual(body["error"], expected)

    def test_sse_stream_delivers_a_snapshot(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/stream?logs=1")
        with urllib.request.urlopen(request, timeout=15) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
            buffer = b""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                chunk = response.read(1024)
                if not chunk:
                    break
                buffer += chunk
                if b"event: snapshot" in buffer:
                    break
        self.assertIn(b"event: snapshot", buffer)
        self.assertIn(b'"tasks"', buffer)


class LockTests(unittest.TestCase):
    def test_rejects_second_monitor_instance_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "monitor.lock"
            first = MonitorInstanceLock(path)
            second = MonitorInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()


if __name__ == "__main__":
    unittest.main()
