"""Read the ChatGPT desktop app's folder (project) list.

The app stores its sidebar projects in the same SQLite database Codex uses:
``~/.codex/state_5.sqlite``.  ``projects`` is ordered by ``position``, which is
exactly the order the sidebar shows, and ``project_roots`` carries the working
directory each folder points at.  ``threads.project_id`` is indexed, so once a
session is created inside a folder it can be looked back up cheaply.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .common import MonitorError, utc_now

DEFAULT_DB_PATH = Path.home() / ".codex" / "state_5.sqlite"
DEFAULT_FOLDER_LIMIT = 10
CACHE_SECONDS = 30.0


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3.0)
    connection.row_factory = sqlite3.Row
    return connection


def _folder_rows(connection: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT p.id AS id, p.name AS name, p.position AS position "
        "FROM projects p ORDER BY p.position LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    folders: list[dict[str, Any]] = []
    for row in rows:
        roots = connection.execute(
            "SELECT path FROM project_roots WHERE project_id = ? ORDER BY position",
            (row["id"],),
        ).fetchall()
        folders.append({
            "id": str(row["id"] or ""),
            "name": str(row["name"] or ""),
            "position": int(row["position"] or 0),
            "roots": [str(item["path"]) for item in roots if str(item["path"] or "").strip()],
        })
    return folders


def _available_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def folder_sessions(
    folder_id: str,
    *,
    db_path: Path | None = None,
    limit: int = 50,
    max_idle_seconds: float = 6 * 3600,
) -> list[dict[str, Any]]:
    """Active sessions that belong to ``folder_id``, newest first.

    Uses ``idx_threads_project_id`` so this stays an index scan even with tens of
    thousands of rows.  A thread whose rollout file has gone quiet for longer
    than ``max_idle_seconds`` is dropped — the database keeps rows for sessions
    that already exited.
    """
    marker = str(folder_id or "").strip()
    if not marker:
        return []
    path = Path(db_path or os.environ.get("CODEX_STATE_DB") or DEFAULT_DB_PATH).expanduser()
    if not path.is_file():
        return []
    cutoff = time.time() - max(60.0, float(max_idle_seconds))
    try:
        connection = _connect(path)
    except sqlite3.Error as exc:
        raise MonitorError(f"无法打开 ChatGPT 文件夹数据库: {exc}") from exc
    try:
        # Older app builds lack some of these columns; select what is there.
        columns = _available_columns(connection, "threads")
        wanted = [name for name in ("id", "cwd", "title", "updated_at_ms", "rollout_path", "first_user_message") if name in columns]
        if not wanted or "id" not in wanted:
            return []
        rows = connection.execute(
            f"SELECT {', '.join(wanted)} FROM threads WHERE project_id = ? AND archived = 0 "
            "ORDER BY updated_at_ms DESC LIMIT ?",
            (marker, max(1, min(int(limit), 500))),
        ).fetchall()
    except sqlite3.Error as exc:
        raise MonitorError(f"查询文件夹会话失败: {exc}") from exc
    finally:
        connection.close()

    sessions: list[dict[str, Any]] = []
    for row in rows:
        record = {key: row[key] for key in row.keys()}
        rollout = Path(str(record.get("rollout_path") or ""))
        try:
            mtime = rollout.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime and mtime < cutoff:
            continue
        sessions.append({
            "threadId": str(record.get("id") or ""),
            "cwd": str(record.get("cwd") or ""),
            "title": str(record.get("title") or ""),
            "rolloutPath": str(rollout),
            "lastActivityAt": mtime,
            "firstUserMessage": str(record.get("first_user_message") or "")[:400],
        })
    return sessions


class FolderProvider:
    """Cached reader for the app's first N folders."""

    def __init__(self, db_path: Path | None = None, *, limit: int = DEFAULT_FOLDER_LIMIT, cache_seconds: float = CACHE_SECONDS):
        self.db_path = Path(db_path or os.environ.get("CODEX_STATE_DB") or DEFAULT_DB_PATH).expanduser()
        self.limit = max(1, int(limit))
        self.cache_seconds = max(1.0, float(cache_seconds))
        self._lock = threading.RLock()
        self._at = 0.0
        self._data: dict[str, Any] = {"items": [], "error": "", "fetchedAt": ""}

    def list(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if not force and self._data.get("fetchedAt") and now - self._at < self.cache_seconds:
                return dict(self._data)
        if not self.db_path.is_file():
            data = {"items": [], "error": f"找不到 ChatGPT 文件夹数据库: {self.db_path}", "fetchedAt": utc_now()}
        else:
            try:
                connection = _connect(self.db_path)
            except sqlite3.Error as exc:
                data = {"items": [], "error": f"无法打开 ChatGPT 文件夹数据库: {exc}", "fetchedAt": utc_now()}
            else:
                try:
                    data = {"items": _folder_rows(connection, self.limit), "error": "", "fetchedAt": utc_now()}
                except sqlite3.Error as exc:
                    data = {"items": [], "error": f"读取文件夹列表失败: {exc}", "fetchedAt": utc_now()}
                finally:
                    connection.close()
        with self._lock:
            self._data = data
            self._at = time.monotonic()
            return dict(data)

    def get(self, folder_id: str, *, force: bool = False) -> dict[str, Any] | None:
        marker = str(folder_id or "").strip()
        if not marker:
            return None
        for item in self.list(force=force).get("items") or []:
            if str(item.get("id") or "") == marker:
                return item
        return None

    def resolve_workdir(self, folder_id: str) -> str:
        """First existing root of a folder, or "" when none can be used."""
        folder = self.get(folder_id)
        if not folder:
            return ""
        for root in folder.get("roots") or []:
            candidate = Path(str(root)).expanduser()
            if candidate.is_dir():
                return str(candidate)
        return ""
