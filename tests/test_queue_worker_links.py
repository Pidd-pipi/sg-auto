"""Tests for the deep-link folder parameter and the quota hand-off."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import queue_worker  # noqa: E402
from api.common import queue_prompt_sha256  # noqa: E402
from queue_log import format_rollout_event, redact_log_text  # noqa: E402


class DeepLinkTests(unittest.TestCase):
    def test_folder_id_is_forwarded_as_project_id(self):
        link = queue_worker.build_deep_link(workdir=Path("/tmp/work"), prompt="hello", folder_id="f-123")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        self.assertEqual(query["path"], ["/tmp/work"])
        self.assertEqual(query["mode"], ["work"])
        self.assertEqual(query["prompt"], ["hello"])
        self.assertEqual(query["projectId"], ["f-123"])

    def test_no_folder_id_means_no_project_id(self):
        link = queue_worker.build_deep_link(workdir=Path("/tmp/work"), prompt="hello")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        self.assertNotIn("projectId", query)
        self.assertEqual(query["path"], ["/tmp/work"])

    def test_prompt_is_byte_identical_for_all_candidates(self):
        prompt = "使用 $sologsb-0917 执行任务"
        first = queue_worker.build_deep_link(workdir=Path("/tmp/work"), prompt=prompt, folder_id="f-1")
        second = queue_worker.build_deep_link(workdir=Path("/tmp/work"), prompt=prompt, folder_id="f-1")
        self.assertEqual(first, second)
        self.assertEqual(queue_prompt_sha256(prompt), queue_prompt_sha256(prompt))


class ResultRecordTests(unittest.TestCase):
    def _args(self, **overrides):
        values = {
            "task_name": "gb-1-20260920-120000-abc",
            "project_code": "gb-1",
            "task_type": "0-1代码生成",
            "difficulty": "困难",
            "side": "both",
            "folder_id": "folder-9",
            "folder_path": "/tmp/work",
            "platform_task_id": "task-77",
            "platform_task_no": "gb-1-代码生成-3",
            "variant_id": "variant-3",
        }
        values.update(overrides)
        return type("Args", (), values)()

    def test_running_result_carries_folder_and_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            queue_worker._write_running_result(
                path,
                stage="desktop-submitted",
                workdir=Path("/tmp/work"),
                task_root=Path("/tmp/work/gb-1-20260920-120000-abc"),
                args=self._args(),
                push_helper=Path("/tmp/CodexQueuePush"),
                trigger_prompt_file=Path("/tmp/prompt.txt"),
                deep_link="codex://threads/new?path=/tmp/work",
                prompt_sha256="a" * 64,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["folderId"], "folder-9")
            self.assertEqual(data["platformTaskId"], "task-77")
            self.assertEqual(data["platformTaskNo"], "gb-1-代码生成-3")
            self.assertEqual(data["variantId"], "variant-3")
            self.assertEqual(data["promptSha256"], "a" * 64)
            self.assertIn("quotaReportedAt", data)


class RolloutFormatTests(unittest.TestCase):
    def test_secrets_are_redacted(self):
        self.assertNotIn("sk-abcdef123456", redact_log_text("key sk-abcdef123456 here"))

    def test_user_messages_without_task_name_are_skipped(self):
        event = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "unrelated"}]}}
        self.assertEqual(format_rollout_event(event, "gb-1-20260920"), [])

    def test_function_call_renders_command(self):
        event = {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({"command": "docker ps"}),
            },
        }
        lines = format_rollout_event(event, "gb-1")
        self.assertEqual(len(lines), 1)
        self.assertIn("docker ps", lines[0])


if __name__ == "__main__":
    unittest.main()
