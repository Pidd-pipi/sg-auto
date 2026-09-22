"""Tests for the ChatGPT folder reader and the scheduler log."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from api.folders import FolderProvider, folder_sessions  # noqa: E402
from api.logs import SchedulerLog  # noqa: E402


def make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE project_roots (
            project_id TEXT NOT NULL, position INTEGER NOT NULL, path TEXT NOT NULL,
            PRIMARY KEY (project_id, position)
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
            cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
            updated_at_ms INTEGER NOT NULL DEFAULT 0, project_id TEXT,
            created_at_ms INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_threads_project_id ON threads(project_id, archived, created_at_ms DESC)
            WHERE project_id IS NOT NULL;
    """)
    folders = [
        ("f-a", "sologsb-0920", 0, ["/tmp/work-a"]),
        ("f-b", "swe-0919", 1, ["/tmp/work-b1", "/tmp/work-b2"]),
        ("f-c", "third", 2, []),
    ]
    for folder_id, name, position, roots in folders:
        connection.execute(
            "INSERT INTO projects (id, name, position, created_at_ms, updated_at_ms) VALUES (?, ?, ?, 0, 0)",
            (folder_id, name, position),
        )
        for index, root in enumerate(roots):
            connection.execute(
                "INSERT INTO project_roots (project_id, position, path) VALUES (?, ?, ?)",
                (folder_id, index, root),
            )
    now_ms = 1_700_000_000_000
    for index, (thread_id, folder_id) in enumerate([("t1", "f-a"), ("t2", "f-a"), ("t3", "f-b")]):
        connection.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd,"
            " title, sandbox_policy, approval_mode, updated_at_ms, project_id) VALUES (?, ?, 0, 0, 'x', 'y',"
            " '/tmp', ?, 'r', 'a', ?, ?)",
            (thread_id, f"/tmp/rollout-{thread_id}.jsonl", thread_id, now_ms + index, folder_id),
        )
    connection.commit()
    connection.close()


class FolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "state_5.sqlite"
        make_db(self.db)

    def test_lists_first_n_in_sidebar_order(self):
        provider = FolderProvider(self.db, limit=2)
        items = provider.list(force=True)["items"]
        self.assertEqual([item["name"] for item in items], ["sologsb-0920", "swe-0919"])
        self.assertEqual(items[1]["roots"], ["/tmp/work-b1", "/tmp/work-b2"])

    def test_missing_db_reports_error(self):
        provider = FolderProvider(self.root / "nope.sqlite")
        payload = provider.list(force=True)
        self.assertEqual(payload["items"], [])
        self.assertIn("找不到", payload["error"])

    def test_resolve_workdir_prefers_existing_root(self):
        # Only roots that exist on disk can be used; the fixture paths do not.
        self.assertEqual(FolderProvider(self.db).resolve_workdir("f-c"), "")
        real = self.root / "real-root"
        real.mkdir()
        connection = sqlite3.connect(self.db)
        connection.execute("INSERT INTO project_roots (project_id, position, path) VALUES ('f-c', 1, ?)", (str(real),))
        connection.commit()
        connection.close()
        # A fresh provider sees the new root and returns the first usable one.
        self.assertEqual(FolderProvider(self.db).resolve_workdir("f-c"), str(real))

    def test_sessions_are_scoped_to_the_folder(self):
        sessions = folder_sessions("f-a", db_path=self.db, max_idle_seconds=10**9)
        self.assertEqual({item["threadId"] for item in sessions}, {"t1", "t2"})
        self.assertEqual(len(folder_sessions("f-c", db_path=self.db)), 0)

    def test_result_is_cached(self):
        provider = FolderProvider(self.db, cache_seconds=30)
        first = provider.list()
        self.assertTrue(first["items"])
        # A second read inside the TTL must not hit the database again.
        self.assertEqual(provider.list(), first)


class SchedulerLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "scheduler.jsonl"
        self.log = SchedulerLog(self.path)

    def test_entries_are_sequential_and_readable(self):
        self.log.emit("service.started", detail="hello")
        entry = self.log.emit("queue.started", level="warning", projectCode="gb-1", detail="PID=1")
        self.assertEqual(entry["seq"], 2)
        self.assertEqual(entry["level"], "warning")
        after = self.log.after(1)
        self.assertEqual([item["event"] for item in after], ["queue.started"])

    def test_seq_survives_restart(self):
        self.log.emit("first")
        self.log.close()
        restarted = SchedulerLog(self.path)
        self.assertEqual(restarted.last_seq(), 1)
        self.assertEqual(restarted.emit("second")["seq"], 2)

    def test_subscribers_receive_entries(self):
        key, queue = self.log.subscribe()
        self.log.emit("ping")
        self.assertEqual(len(queue), 1)
        self.log.unsubscribe(key)
        self.log.emit("pong")
        self.assertEqual(len(queue), 1)

    def test_secrets_are_redacted(self):
        entry = self.log.emit("test", detail="token=abcdef123456")
        self.assertNotIn("abcdef123456", entry["detail"])

    def test_recent_filters_by_level(self):
        self.log.emit("a", level="info")
        self.log.emit("b", level="error")
        self.assertEqual([i["event"] for i in self.log.recent(level="error")], ["b"])


if __name__ == "__main__":
    unittest.main()
