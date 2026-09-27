from __future__ import annotations

import os
import json
import sqlite3
import time
import re
from dataclasses import dataclass
from pathlib import Path

MAX_PENDING_QUEUE = 8


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class BusyInfo:
    active_task_id: str | None
    queue_length: int
    retry_after_seconds: int
    reason: str = "CHATGPT_BROWSER_BUSY"

    def as_dict(self) -> dict[str, object]:
        message = (
            "共享 ChatGPT 浏览器任务队列已满。"
            if self.reason == "CHATGPT_QUEUE_FULL"
            else "共享 ChatGPT 浏览器当前已有任务，占用槽位只有一个。"
        )
        return {
            "accepted": False,
            "state": "BUSY",
            "error": {
                "code": self.reason,
                "message": message,
            },
            "retryable": True,
            "retry_after_seconds": self.retry_after_seconds,
            "active_task_id": self.active_task_id,
            "queue_length": self.queue_length,
            "suggested_action": "稍后重试，或将 on_busy=queue 让服务端排队。",
        }


class SchedulerBusy(RuntimeError):
    def __init__(self, info: BusyInfo):
        super().__init__(info.reason)
        self.info = info


class IdempotencyConflict(ValueError):
    pass


class Scheduler:
    """Durable single-slot scheduler shared by stdio and HTTP MCP processes."""

    def __init__(
        self,
        jobs_dir: Path,
        *,
        export_audit_retention_seconds: float = 90 * 24 * 60 * 60,
    ):
        self.db_path = jobs_dir / "scheduler.sqlite3"
        self.export_audit_retention_seconds = export_audit_retention_seconds
        jobs_dir.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    task_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    request_id TEXT UNIQUE,
                    request_hash TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    task_id TEXT,
                    owner_pid INTEGER,
                    owner_token TEXT,
                    state TEXT,
                    heartbeat REAL
                );
                CREATE TABLE IF NOT EXISTS export_jobs (
                    export_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    conversation_url TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    legacy_inline INTEGER NOT NULL DEFAULT 0,
                    request_key TEXT UNIQUE,
                    input_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    source TEXT,
                    completeness_json TEXT,
                    reason_json TEXT,
                    message_count INTEGER,
                    attachment_count INTEGER,
                    views_json TEXT,
                    error_code TEXT,
                    error_detail TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS export_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    export_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    event TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                INSERT OR IGNORE INTO slot(id) VALUES (1);
            """
        )
        queue_columns = {row["name"] for row in conn.execute("PRAGMA table_info(queue)")}
        if "request_hash" not in queue_columns:
            conn.execute("ALTER TABLE queue ADD COLUMN request_hash TEXT")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(slot)").fetchall()}
        if "owner_token" not in columns:
            conn.execute("ALTER TABLE slot ADD COLUMN owner_token TEXT")
        export_columns = {row[1] for row in conn.execute("PRAGMA table_info(export_jobs)").fetchall()}
        if "legacy_inline" not in export_columns:
            conn.execute("ALTER TABLE export_jobs ADD COLUMN legacy_inline INTEGER NOT NULL DEFAULT 0")
        if "page_requests" not in export_columns:
            conn.execute("ALTER TABLE export_jobs ADD COLUMN page_requests INTEGER NOT NULL DEFAULT 0")

    def enqueue_export(
        self,
        export_id: str,
        *,
        session_id: str,
        conversation_url: str,
        scope: str,
        count: int,
        request_key: str | None,
        input_fingerprint: str,
        on_busy: str,
        retry_after_seconds: int,
        legacy_inline: bool = False,
    ) -> dict[str, object]:
        if on_busy not in {"reject", "queue"}:
            raise ValueError("on_busy must be reject or queue")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if request_key:
                existing = conn.execute(
                    "SELECT export_id,state,input_fingerprint FROM export_jobs WHERE request_key=?",
                    (request_key,),
                ).fetchone()
                if existing:
                    if existing["input_fingerprint"] != input_fingerprint:
                        raise IdempotencyConflict("request_id was already used with different export inputs")
                    return {
                        "accepted": True,
                        "idempotent": True,
                        "task_id": existing["export_id"],
                        "state": existing["state"],
                    }

            busy = self._busy_info(conn, retry_after_seconds)
            if (busy.active_task_id or busy.queue_length > 0) and on_busy == "reject":
                raise SchedulerBusy(busy)
            if busy.queue_length >= MAX_PENDING_QUEUE:
                raise SchedulerBusy(
                    BusyInfo(busy.active_task_id, busy.queue_length, retry_after_seconds, "CHATGPT_QUEUE_FULL")
                )
            seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM queue").fetchone()[0])
            now = time.time()
            conn.execute(
                "INSERT INTO queue(task_id,seq,state,mode,request_id,created_at) VALUES(?,?,?,?,NULL,?)",
                (export_id, seq, "QUEUED", "export", now),
            )
            conn.execute(
                "INSERT INTO export_jobs(export_id,session_id,conversation_url,scope,count,legacy_inline,request_key,input_fingerprint,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,'QUEUED',?,?)",
                (
                    export_id,
                    session_id,
                    conversation_url,
                    scope,
                    count,
                    int(legacy_inline),
                    request_key,
                    input_fingerprint,
                    now,
                    now,
                ),
            )
            return {
                "accepted": True,
                "idempotent": False,
                "task_id": export_id,
                "state": "QUEUED",
                "queue_position": self._queue_position_conn(conn, export_id),
                "queued_behind": busy.active_task_id,
            }

    def get_export(self, export_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM export_jobs WHERE export_id=?", (export_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for column, key in (
            ("completeness_json", "completeness"),
            ("reason_json", "reason"),
            ("views_json", "views"),
        ):
            raw = result.pop(column, None)
            result[key] = json.loads(raw) if raw else None
        return result

    def list_exports(
        self,
        *,
        states: tuple[str, ...] | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT export_id FROM export_jobs{where} ORDER BY created_at",
                params,
            ).fetchall()
        return [value for row in rows if (value := self.get_export(str(row["export_id"]))) is not None]

    def update_export(self, export_id: str, **values: object) -> None:
        columns = {
            "session_id": "session_id",
            "conversation_url": "conversation_url",
            "scope": "scope",
            "count": "count",
            "state": "state",
            "source": "source",
            "message_count": "message_count",
            "attachment_count": "attachment_count",
            "error_code": "error_code",
            "error_detail": "error_detail",
            "expires_at": "expires_at",
            "page_requests": "page_requests",
        }
        json_columns = {
            "completeness": "completeness_json",
            "reason": "reason_json",
            "views": "views_json",
        }
        assignments: list[str] = []
        params: list[object] = []
        for key, value in values.items():
            if key in columns:
                assignments.append(f"{columns[key]}=?")
                params.append(value)
            elif key in json_columns:
                assignments.append(f"{json_columns[key]}=?")
                params.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            else:
                raise ValueError(f"unsupported export field: {key}")
        if not assignments:
            return
        assignments.append("updated_at=?")
        params.extend((time.time(), export_id))
        with self._connect() as conn:
            conn.execute(
                f"UPDATE export_jobs SET {','.join(assignments)} WHERE export_id=?",
                params,
            )

    def record_export_event(
        self,
        export_id: str,
        event: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        allowed = {
            "source",
            "api_statuses",
            "source_summary",
            "message_count",
            "attachment_count",
            "messages_completeness",
            "attachments_completeness",
            "overall_completeness",
            "bytes",
            "sha256",
            "error_code",
            "error_type",
            "reason",
            "view",
            "byte_offset",
            "returned_bytes",
            "message_offset",
            "returned_count",
            "page_request_count",
            "message_segment",
            "eof",
            "recovery_reason",
            "operation",
        }
        safe: dict[str, object] = {}
        for key, value in (metadata or {}).items():
            if key not in allowed:
                continue
            if key == "api_statuses" and isinstance(value, list):
                safe_attempts: list[dict[str, object]] = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    attempt: dict[str, object] = {}
                    for field in ("route", "error_type", "metadata_status", "direction"):
                        item_value = item.get(field)
                        if isinstance(item_value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item_value):
                            attempt[field] = item_value
                    for field in ("status", "message_count", "retry_attempt"):
                        item_value = item.get(field)
                        if isinstance(item_value, int) and not isinstance(item_value, bool) and item_value >= 0:
                            attempt[field] = item_value
                    for field in ("download_url_present", "has_previous_page", "has_next_page"):
                        item_value = item.get(field)
                        if isinstance(item_value, bool):
                            attempt[field] = item_value
                    query_keys = item.get("query_keys")
                    if isinstance(query_keys, list) and len(query_keys) <= 32:
                        safe_keys = [
                            key_name
                            for key_name in query_keys
                            if isinstance(key_name, str)
                            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", key_name)
                        ]
                        if len(safe_keys) == len(query_keys):
                            attempt["query_keys"] = safe_keys
                    safe_attempts.append(attempt)
                safe[key] = safe_attempts
            elif key == "source_summary" and isinstance(value, dict):
                summary: dict[str, object] = {}
                count_fields = {
                    "api_page_count",
                    "api_raw_message_count",
                    "duplicate_message_count",
                    "excluded_system_message_count",
                    "interpreter_metadata_count",
                    "interpreter_metadata_failures",
                    "interpreter_metadata_response_count",
                    "interpreter_metadata_resource_count",
                    "interpreter_metadata_refresh_count",
                    "file_download_metadata_count",
                    "file_download_reference_count",
                    "file_download_metadata_attempt_count",
                    "file_download_resource_count",
                    "file_download_refresh_count",
                }
                for field in count_fields:
                    item_value = value.get(field)
                    if isinstance(item_value, int) and not isinstance(item_value, bool) and item_value >= 0:
                        summary[field] = item_value
                for field in ("interpreter_metadata_quiet",):
                    item_value = value.get(field)
                    if isinstance(item_value, bool):
                        summary[field] = item_value
                resource_error = value.get("interpreter_metadata_resource_error")
                if isinstance(resource_error, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", resource_error):
                    summary["interpreter_metadata_resource_error"] = resource_error

                references = value.get("api_attachment_reference_summary")
                if isinstance(references, dict):
                    safe_references: dict[str, object] = {}
                    for field in (
                        "attachment_reference_count",
                        "attachment_file_id_count",
                        "attachment_sandbox_path_count",
                        "library_file_id_count",
                        "library_file_id_with_file_id_count",
                        "library_file_id_matching_file_id_count",
                        "library_file_id_pointer_overlap_count",
                        "file_id_pointer_overlap_count",
                        "pointer_reference_count",
                        "library_file_id_with_sandbox_path_count",
                        "rich_part_count",
                        "rich_part_file_id_count",
                        "rich_part_sandbox_path_count",
                    ):
                        item_value = references.get(field)
                        if isinstance(item_value, int) and not isinstance(item_value, bool) and item_value >= 0:
                            safe_references[field] = item_value
                    content_types = references.get("rich_part_content_type_counts")
                    if isinstance(content_types, dict) and len(content_types) <= 64:
                        safe_types: dict[str, int] = {}
                        for content_type, item_value in content_types.items():
                            if (
                                isinstance(content_type, str)
                                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", content_type)
                                and isinstance(item_value, int)
                                and not isinstance(item_value, bool)
                                and item_value >= 0
                            ):
                                safe_types[content_type] = item_value
                        if len(safe_types) == len(content_types):
                            safe_references["rich_part_content_type_counts"] = safe_types
                    if safe_references:
                        summary["api_attachment_reference_summary"] = safe_references

                scan = value.get("attachment_scan")
                if isinstance(scan, dict):
                    safe_scan: dict[str, object] = {}
                    for field in (
                        "iterations",
                        "stable_rounds",
                        "rendered_message_count",
                        "observed_message_id_count",
                        "expected_api_message_count",
                        "scroller_count",
                        "max_scroll",
                        "ui_page_response_count",
                        "ui_older_page_response_count",
                        "ui_page_message_count",
                        "scan_pass_count",
                    ):
                        item_value = scan.get(field)
                        if isinstance(item_value, int) and not isinstance(item_value, bool) and item_value >= 0:
                            safe_scan[field] = item_value
                    for field in ("stable", "reached_end", "rendered_count_reaches_api_count"):
                        item_value = scan.get(field)
                        if isinstance(item_value, bool):
                            safe_scan[field] = item_value
                    for field in ("stop_reason", "error_type"):
                        item_value = scan.get(field)
                        if isinstance(item_value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", item_value):
                            safe_scan[field] = item_value
                    if safe_scan:
                        summary["attachment_scan"] = safe_scan

                pagination = value.get("pagination")
                if isinstance(pagination, dict):
                    safe_pagination: dict[str, object] = {}
                    for field in ("page_count", "raw_message_count", "duplicate_count", "cursor_mismatch_count"):
                        item_value = pagination.get(field)
                        if isinstance(item_value, int) and not isinstance(item_value, bool) and item_value >= 0:
                            safe_pagination[field] = item_value
                    conflicting_duplicate = pagination.get("conflicting_duplicate")
                    if isinstance(conflicting_duplicate, bool):
                        safe_pagination["conflicting_duplicate"] = conflicting_duplicate
                    for field in ("completeness", "reason"):
                        item_value = pagination.get(field)
                        if isinstance(item_value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", item_value):
                            safe_pagination[field] = item_value
                    flags = pagination.get("page_flags")
                    if isinstance(flags, list) and flags:
                        boundaries = [flags[0]] if len(flags) == 1 else [flags[0], flags[-1]]
                        safe_flags = []
                        for item in boundaries:
                            if not isinstance(item, dict):
                                continue
                            safe_flags.append(
                                {
                                    field: item[field]
                                    for field in ("has_previous_page", "has_next_page")
                                    if isinstance(item.get(field), bool)
                                }
                            )
                        if len(safe_flags) == len(boundaries):
                            safe_pagination["page_boundaries"] = safe_flags
                    if safe_pagination:
                        summary["pagination"] = safe_pagination
                if summary:
                    safe[key] = summary
            elif key in {
                "message_count",
                "attachment_count",
                "bytes",
                "byte_offset",
                "returned_bytes",
                "message_offset",
                "returned_count",
                "page_request_count",
            }:
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    safe[key] = value
            elif key in {"eof", "message_segment"} and isinstance(value, bool):
                safe[key] = value
            elif key == "sha256" and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                safe[key] = value
            elif key in {"messages_completeness", "attachments_completeness", "overall_completeness"}:
                if value in {"verified", "unverified", "partial"}:
                    safe[key] = value
            elif isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value):
                safe[key] = value
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO export_events(export_id,created_at,event,metadata_json) VALUES(?,?,?,?)",
                (export_id, time.time(), event, json.dumps(safe, ensure_ascii=False, separators=(",", ":"))),
            )

    def export_events(self, export_id: str) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT created_at,event,metadata_json FROM export_events WHERE export_id=? ORDER BY event_id",
                (export_id,),
            ).fetchall()
        return [
            {
                "created_at": row["created_at"],
                "event": row["event"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def record_export_page_request(
        self,
        export_id: str,
    ) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state,page_requests FROM export_jobs WHERE export_id=?",
                (export_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"export not found: {export_id}")
            if row["state"] != "READY":
                raise ValueError("page requests require a READY export")
            current = int(row["page_requests"])
            if current < 0:
                raise RuntimeError("export page request count is invalid")
            if current == 2**63 - 1:
                raise OverflowError("export page request counter exhausted")
            updated = current + 1
            conn.execute(
                "UPDATE export_jobs SET page_requests=?,updated_at=? WHERE export_id=?",
                (updated, time.time(), export_id),
            )
            return updated

    def prune_export_history(self, *, now: float | None = None) -> dict[str, int]:
        current_time = time.time() if now is None else now
        cutoff = current_time - self.export_audit_retention_seconds
        active_states = ("QUEUED", "RUNNING", "CANCEL_REQUESTED")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old_jobs = conn.execute(
                "SELECT export_id FROM export_jobs WHERE created_at<? AND state NOT IN (?,?,?)",
                (cutoff, *active_states),
            ).fetchall()
            old_ids = [str(row["export_id"]) for row in old_jobs]
            queue_rows = 0
            jobs = 0
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                queue_rows = conn.execute(
                    f"DELETE FROM queue WHERE mode='export' AND task_id IN ({placeholders})",
                    old_ids,
                ).rowcount
                jobs = conn.execute(
                    f"DELETE FROM export_jobs WHERE export_id IN ({placeholders})",
                    old_ids,
                ).rowcount
            events = conn.execute("DELETE FROM export_events WHERE created_at<?", (cutoff,)).rowcount
        return {"jobs": jobs, "events": events, "queue_rows": queue_rows}

    def _queue_length(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM queue WHERE state IN ('ADMITTING','QUEUED')").fetchone()[0])

    def _busy_info(self, conn: sqlite3.Connection, retry_after_seconds: int) -> BusyInfo:
        row = conn.execute("SELECT task_id, state FROM slot WHERE id=1").fetchone()
        active = row["task_id"] if row and row["task_id"] else None
        reason = "CHATGPT_BROWSER_BUSY" if active else "CHATGPT_QUEUE_BACKLOG"
        return BusyInfo(active, self._queue_length(conn), retry_after_seconds, reason)

    def enqueue(
        self,
        task_id: str,
        mode: str,
        *,
        on_busy: str,
        retry_after_seconds: int,
        request_id: str | None = None,
        request_hash: str | None = None,
    ) -> dict[str, object]:
        if on_busy not in {"reject", "queue"}:
            raise ValueError("on_busy must be reject or queue")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if request_id:
                existing = conn.execute(
                    "SELECT task_id, state, request_hash FROM queue WHERE request_id=?", (request_id,)
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict("request_id was already used with different task inputs")
                    return {
                        "accepted": True,
                        "idempotent": True,
                        "task_id": existing["task_id"],
                        "state": existing["state"],
                    }
            busy = self._busy_info(conn, retry_after_seconds)
            has_queued = busy.queue_length > 0
            if (busy.active_task_id or has_queued) and on_busy == "reject":
                raise SchedulerBusy(busy)
            if busy.queue_length >= MAX_PENDING_QUEUE:
                raise SchedulerBusy(
                    BusyInfo(busy.active_task_id, busy.queue_length, retry_after_seconds, "CHATGPT_QUEUE_FULL")
                )
            seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM queue").fetchone()[0])
            conn.execute(
                "INSERT INTO queue(task_id,seq,state,mode,request_id,request_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, seq, "ADMITTING", mode, request_id, request_hash, time.time()),
            )
            return {
                "accepted": True,
                "idempotent": False,
                "task_id": task_id,
                "state": "QUEUED",
                "queue_position": self._queue_position_conn(conn, task_id),
                "queued_behind": busy.active_task_id,
            }

    def _queue_position_conn(self, conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute("SELECT seq FROM queue WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return 0
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM queue WHERE state IN ('ADMITTING','QUEUED') AND seq<=?", (row["seq"],)
            ).fetchone()[0]
        )

    def reserve_next(self, owner_pid: int, owner_token: str, *, allow_exports: bool = True) -> str | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            slot = conn.execute("SELECT * FROM slot WHERE id=1").fetchone()
            if slot["task_id"]:
                if slot["owner_token"] == owner_token:
                    return slot["task_id"]
                if _pid_alive(slot["owner_pid"]):
                    return None
                current = conn.execute(
                    "SELECT state FROM queue WHERE task_id=?", (slot["task_id"],)
                ).fetchone()
                if current and current["state"] == "RUNNING":
                    conn.execute("UPDATE queue SET state='QUEUED' WHERE task_id=?", (slot["task_id"],))
                    conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1")
                elif current and current["state"] == "STARTING":
                    conn.execute("UPDATE queue SET state='RECOVERY_REQUIRED' WHERE task_id=?", (slot["task_id"],))
                    return None
                else:
                    conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1")
            row = conn.execute(
                "SELECT task_id,mode FROM queue WHERE state='QUEUED' ORDER BY seq LIMIT 1"
            ).fetchone()
            if not row:
                return None
            if row["mode"] == "export" and not allow_exports:
                return None
            task_id = row["task_id"]
            conn.execute(
                "UPDATE queue SET state='STARTING' WHERE task_id=?", (task_id,)
            )
            conn.execute(
                "UPDATE slot SET task_id=?,owner_pid=?,owner_token=?,state='STARTING',heartbeat=? WHERE id=1",
                (task_id, owner_pid, owner_token, time.time()),
            )
            return task_id

    def set_state(self, task_id: str, state: str, *, owner_pid: int | None = None) -> None:
        with self._connect() as conn:
            if owner_pid is None:
                conn.execute("UPDATE queue SET state=? WHERE task_id=?", (state, task_id))
            else:
                conn.execute(
                    "UPDATE queue SET state=? WHERE task_id=? AND EXISTS (SELECT 1 FROM slot WHERE id=1 AND task_id=? AND owner_pid=?)",
                    (state, task_id, task_id, owner_pid),
                )
            conn.execute(
                "UPDATE slot SET state=?,heartbeat=? WHERE id=1 AND task_id=?",
                (state, time.time(), task_id),
            )

    def heartbeat(self, task_id: str, owner_token: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE slot SET heartbeat=? WHERE id=1 AND task_id=? AND owner_token=?",
                (time.time(), task_id, owner_token),
            )

    def acquire_external_slot(self, task_id: str, owner_pid: int, owner_token: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            slot = conn.execute("SELECT task_id FROM slot WHERE id=1").fetchone()
            if (slot and slot["task_id"]) or self._queue_length(conn):
                return False
            conn.execute(
                "UPDATE slot SET task_id=?,owner_pid=?,owner_token=?,state='FETCHING',heartbeat=? WHERE id=1",
                (task_id, owner_pid, owner_token, time.time()),
            )
            return True

    def release_external_slot(self, task_id: str, owner_token: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1 AND task_id=? AND owner_token=?",
                (task_id, owner_token),
            )

    def complete(self, task_id: str, state: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE queue SET state=? WHERE task_id=?", (state, task_id))
            conn.execute(
                "UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1 AND task_id=?",
                (task_id,),
            )

    def request_cancel(self, task_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT state FROM queue WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return "MISSING"
            if row["state"] == "QUEUED":
                conn.execute("UPDATE queue SET state='CANCELLED' WHERE task_id=?", (task_id,))
                return "CANCELLED"
            conn.execute("UPDATE queue SET state='CANCEL_REQUESTED' WHERE task_id=?", (task_id,))
            conn.execute("UPDATE slot SET state='CANCEL_REQUESTED',heartbeat=? WHERE id=1 AND task_id=?", (time.time(), task_id))
            return "CANCEL_REQUESTED"

    def status(self, task_id: str | None = None) -> dict[str, object]:
        with self._connect() as conn:
            slot = conn.execute("SELECT * FROM slot WHERE id=1").fetchone()
            result: dict[str, object] = {
                "active_task_id": slot["task_id"] if slot else None,
                "active_state": slot["state"] if slot else None,
                "queue_length": self._queue_length(conn),
            }
            if task_id:
                row = conn.execute("SELECT state FROM queue WHERE task_id=?", (task_id,)).fetchone()
                result["scheduler_state"] = row["state"] if row else None
                result["queue_position"] = self._queue_position_conn(conn, task_id) if row else None
            return result

    def recover_startup(self, store: object) -> None:
        """Recover a dead RUNNING worker without resubmitting its prompt."""
        owner_pid = os.getpid()
        with self._connect() as conn:
            slot = conn.execute("SELECT * FROM slot WHERE id=1").fetchone()
            if not slot or not slot["task_id"] or _pid_alive(slot["owner_pid"]) or slot["owner_pid"] == owner_pid:
                return
            row = conn.execute("SELECT state,mode FROM queue WHERE task_id=?", (slot["task_id"],)).fetchone()
            if row and row["mode"] == "export" and row["state"] in {"STARTING", "RUNNING"}:
                conn.execute("UPDATE queue SET state='QUEUED' WHERE task_id=?", (slot["task_id"],))
                export = conn.execute("SELECT 1 FROM export_jobs WHERE export_id=?", (slot["task_id"],)).fetchone()
                if export:
                    conn.execute(
                        "UPDATE export_jobs SET state='QUEUED',updated_at=? WHERE export_id=?",
                        (time.time(), slot["task_id"]),
                    )
                    conn.execute(
                        "INSERT INTO export_events(export_id,created_at,event,metadata_json) VALUES(?,?,?,?)",
                        (
                            slot["task_id"],
                            time.time(),
                            "REQUEUED_AFTER_RESTART",
                            json.dumps({"recovery_reason": "worker_process_stopped"}),
                        ),
                    )
                conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1")
            elif row and row["mode"] == "export" and row["state"] == "CANCEL_REQUESTED":
                conn.execute("UPDATE queue SET state='CANCELLED' WHERE task_id=?", (slot["task_id"],))
                conn.execute(
                    "UPDATE export_jobs SET state='CANCELLED',updated_at=? WHERE export_id=?",
                    (time.time(), slot["task_id"]),
                )
                conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1")
            elif row and row["state"] == "RUNNING":
                conn.execute("UPDATE queue SET state='QUEUED' WHERE task_id=?", (slot["task_id"],))
                conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1")
            elif row and row["state"] == "STARTING":
                conn.execute("UPDATE queue SET state='RECOVERY_REQUIRED' WHERE task_id=?", (slot["task_id"],))
                conn.execute("UPDATE slot SET state='RECOVERY_REQUIRED',heartbeat=? WHERE id=1", (time.time(),))
