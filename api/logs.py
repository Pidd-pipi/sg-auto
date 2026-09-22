"""Global scheduler log: structured JSONL on disk plus an in-process fan-out.

The old implementation had no real scheduler log — only a 32 MB
``monitor.log`` that was never rotated and a 120-entry ring buffer inside
``auto.json``.  Everything here is one line per event with a monotonic ``seq``
so SSE clients can ask for "everything after N".
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .common import SCHEDULER_LOG_PATH, redact_text, utc_now

MAX_BYTES = 50 * 1024 * 1024
MAX_AGE_SECONDS = 7 * 24 * 3600
MEMORY_LIMIT = 2000
LEVELS = ("debug", "info", "warning", "error")


def _rotation_target(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.{stamp}")


class SchedulerLog:
    def __init__(self, path: Path | None = None, *, memory_limit: int = MEMORY_LIMIT):
        self.path = Path(path or SCHEDULER_LOG_PATH)
        self.memory_limit = max(100, int(memory_limit))
        self._lock = threading.RLock()
        self._seq = 0
        self._memory: deque[dict[str, Any]] = deque(maxlen=self.memory_limit)
        self._subscribers: list[tuple[int, "deque[dict[str, Any]]"]] = []
        self._next_subscriber_id = 1
        self._handle = None
        self._written_bytes = 0
        self._opened_at = 0.0
        self._recover_seq()

    # -- lifecycle -------------------------------------------------------- #
    def _recover_seq(self) -> None:
        try:
            if not self.path.is_file():
                return
            self._written_bytes = self.path.stat().st_size
            with self.path.open("rb") as handle:
                handle.seek(max(0, self._written_bytes - 256 * 1024))
                tail = handle.read().decode("utf-8", errors="replace")
            for line in tail.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    try:
                        self._seq = max(self._seq, int(entry.get("seq") or 0))
                    except (TypeError, ValueError):
                        continue
                    self._memory.append(entry)
        except OSError:
            return

    def _ensure_open(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        try:
            self._written_bytes = self.path.stat().st_size
        except OSError:
            self._written_bytes = 0
        self._opened_at = time.time()

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except OSError:
                    pass

    # -- rotation --------------------------------------------------------- #
    def _should_rotate(self) -> bool:
        if self._written_bytes >= MAX_BYTES:
            return True
        return self._opened_at and (time.time() - self._opened_at) >= MAX_AGE_SECONDS

    def _rotate_locked(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.flush()
                handle.close()
            except OSError:
                pass
        try:
            os.replace(self.path, _rotation_target(self.path))
        except OSError:
            pass
        self._written_bytes = 0
        self._opened_at = 0.0
        self._prune_old_files()

    def _prune_old_files(self) -> None:
        cutoff = time.time() - MAX_AGE_SECONDS
        try:
            candidates = sorted(self.path.parent.glob(f"{self.path.name}.*"))
        except OSError:
            return
        for candidate in candidates:
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue

    # -- writing ---------------------------------------------------------- #
    def emit(
        self,
        event: str,
        *,
        level: str = "info",
        taskId: str = "",
        projectCode: str = "",
        detail: Any = "",
        **extra: Any,
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry: dict[str, Any] = {
                "seq": self._seq,
                "ts": utc_now(),
                "level": level if level in LEVELS else "info",
                "event": str(event or "log"),
                "taskId": str(taskId or ""),
                "projectCode": str(projectCode or ""),
                "detail": redact_text(detail, 800) if not isinstance(detail, (dict, list)) else detail,
            }
            for key, value in extra.items():
                if value is not None:
                    entry[key] = value
            self._ensure_open()
            if self._handle is not None:
                try:
                    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
                    self._handle.write(line)
                    self._handle.flush()
                    self._written_bytes += len(line.encode("utf-8"))
                except OSError:
                    pass
            if self._should_rotate():
                self._rotate_locked()
                self._ensure_open()
            self._memory.append(entry)
            self._dispatch_locked(entry)
            return entry

    # -- reading ---------------------------------------------------------- #
    def recent(self, limit: int = 200, *, level: str = "") -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._memory)
        if level and level in LEVELS:
            items = [item for item in items if str(item.get("level") or "") == level]
        return items[-max(1, int(limit)):]

    def after(self, seq: int, limit: int = 500) -> list[dict[str, Any]]:
        """Entries newer than ``seq`` (memory ring, then the tail of the file)."""
        try:
            marker = int(seq)
        except (TypeError, ValueError):
            marker = 0
        with self._lock:
            items = [item for item in self._memory if int(item.get("seq") or 0) > marker]
        if len(items) >= max(1, int(limit)):
            return items[-max(1, int(limit)):]
        oldest_memory = int(self._memory[0].get("seq") or 0) if self._memory else 0
        if marker + 1 < oldest_memory:
            items = self._read_file_after(marker, limit)
        return items[-max(1, int(limit)):]

    def _read_file_after(self, seq: int, limit: int) -> list[dict[str, Any]]:
        try:
            if not self.path.is_file():
                return []
            size = self.path.stat().st_size
            with self.path.open("rb") as handle:
                handle.seek(max(0, size - 4 * 1024 * 1024))
                raw = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        result: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and int(entry.get("seq") or 0) > seq:
                result.append(entry)
        return result[-max(1, int(limit)):]

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    # -- subscriptions ---------------------------------------------------- #
    def subscribe(self, maxsize: int = 1000) -> tuple[int, "deque[dict[str, Any]]"]:
        queue: deque[dict[str, Any]] = deque(maxlen=max(1, int(maxsize)))
        with self._lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers.append((subscriber_id, queue))
        return subscriber_id, queue

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._lock:
            self._subscribers = [
                (key, queue) for key, queue in self._subscribers if key != subscriber_id
            ]

    def _dispatch_locked(self, entry: dict[str, Any]) -> None:
        for _key, queue in list(self._subscribers):
            try:
                queue.append(entry)
            except Exception:
                continue

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
