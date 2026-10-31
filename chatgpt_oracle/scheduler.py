from __future__ import annotations

import os
import json
import sqlite3
import time
import re
from dataclasses import dataclass
from pathlib import Path

MAX_PENDING_QUEUE = 8
MAX_SESSION_SLOTS = 4
_LEGACY_SESSION = "__legacy__"
_WAITING_STATES = ("ADMITTING", "QUEUED")
_ACTIVE_STATES = ("STARTING", "RUNNING", "CANCEL_REQUESTED")


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
            "浏览器诊断当前没有可安全使用的独立槽位；未读取或操作现有 Session。"
            if self.reason == "BROWSER_DIAGNOSTIC_BUSY"
            else
            "共享浏览器任务队列已满。"
            if self.reason == "CHATGPT_QUEUE_FULL"
            else "该 Session 正在处理工作，或四个 Session 槽位均已占用。"
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
            "suggested_action": (
                "稍后重试；保留正在运行的 Session、问题和草稿。"
                if self.reason == "BROWSER_DIAGNOSTIC_BUSY"
                else "稍后重试，或将 on_busy=queue 让服务端排队。"
            ),
        }


class SchedulerBusy(RuntimeError):
    def __init__(self, info: BusyInfo):
        super().__init__(info.reason)
        self.info = info


class IdempotencyConflict(ValueError):
    pass


class _SchedulerConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Scheduler:
    """Four persistent session slots, with FIFO operations inside each session."""

    def __init__(
        self,
        jobs_dir: Path,
        *,
        export_audit_retention_seconds: float = 90 * 24 * 60 * 60,
        shared_pool_path: Path | None = None,
        namespace: str = "",
    ):
        self.db_path = jobs_dir / "scheduler.sqlite3"
        self.export_audit_retention_seconds = export_audit_retention_seconds
        self._pool_path: Path | None = None
        self.namespace = namespace if shared_pool_path is not None else ""
        jobs_dir.mkdir(parents=True, exist_ok=True)
        self._init()
        if shared_pool_path is not None:
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", namespace):
                raise ValueError("Shared browser scheduling requires a provider namespace")
            self._init_shared_pool(shared_pool_path.resolve())
            self._pool_path = shared_pool_path.resolve()

    def _session_key(self, session_id: str | None) -> str:
        key = session_id or _LEGACY_SESSION
        return f"{self.namespace}:{key}" if self.namespace else key

    def _local_key(self, key: str) -> str:
        prefix = self.namespace + ":"
        return key[len(prefix):] if self.namespace and key.startswith(prefix) else key

    def _owns_key(self, key: str) -> bool:
        return not self.namespace or key.startswith(self.namespace + ":")

    @property
    def _legacy_key(self) -> str:
        return self._session_key(None)

    def _init_shared_pool(self, path: Path) -> None:
        """Share admission leases; provider queue/export tables stay private.

        Main contains only slot/session_slots. SQLite resolves the remaining
        table names in the attached provider database, so workers cannot claim
        or reconcile the other provider's task or export envelopes.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=10, isolation_level=None, factory=_SchedulerConnection) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("ATTACH DATABASE ? AS provider", (str(self.db_path),))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in ("slot", "session_slots"):
                    schema = conn.execute("SELECT sql FROM provider.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                    conn.execute(schema.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
                conn.execute("INSERT OR IGNORE INTO main.slot(id) VALUES(1)")
                conn.execute("CREATE TABLE IF NOT EXISTS provider.shared_pool_config (id INTEGER PRIMARY KEY CHECK(id=1), namespace TEXT NOT NULL, path TEXT NOT NULL)")
                previous = conn.execute("SELECT namespace,path FROM provider.shared_pool_config WHERE id=1").fetchone()
                if previous:
                    if previous["namespace"] != self.namespace or Path(previous["path"]) != path:
                        raise ValueError("Existing scheduler belongs to another shared browser pool; migrate it explicitly")
                else:
                    if conn.execute("SELECT 1 FROM provider.slot WHERE task_id IS NOT NULL").fetchone() or conn.execute("SELECT 1 FROM provider.session_slots WHERE task_id IS NOT NULL").fetchone():
                        raise RuntimeError("Stop active browser operations before migrating to the shared pool")
                    local_slots = list(conn.execute("SELECT * FROM provider.session_slots"))
                    existing = {row[0] for row in conn.execute("SELECT session_key FROM main.session_slots")}
                    additions = {self._session_key(row["session_key"]) for row in local_slots} - existing
                    if len(existing) + len(additions) > MAX_SESSION_SLOTS:
                        raise RuntimeError("Close excess resident Sessions before merging the browser pools")
                    conn.execute("UPDATE provider.queue SET session_key=? || session_key", (self.namespace + ":",))
                    for row in local_slots:
                        conn.execute("INSERT OR IGNORE INTO main.session_slots(session_key,persistent,state) VALUES(?,?,?)",
                                     (self._session_key(row["session_key"]), row["persistent"], row["state"]))
                    conn.execute("INSERT INTO provider.shared_pool_config(id,namespace,path) VALUES(1,?,?)", (self.namespace, str(path)))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._pool_path or self.db_path, timeout=10, isolation_level=None, factory=_SchedulerConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        if self._pool_path is not None:
            conn.execute("ATTACH DATABASE ? AS provider", (str(self.db_path),))
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
                CREATE TABLE IF NOT EXISTS session_slots (
                    session_key TEXT PRIMARY KEY,
                    persistent INTEGER NOT NULL,
                    task_id TEXT,
                    owner_pid INTEGER,
                    owner_token TEXT,
                    state TEXT,
                    heartbeat REAL
                );
            """
            )
            # The read/alter sequence must be one transaction when HTTP and
            # stdio initialize the same profile concurrently.
            conn.execute("BEGIN IMMEDIATE")
            queue_columns = {row["name"] for row in conn.execute("PRAGMA table_info(queue)")}
            for name, declaration in (
                ("request_hash", "TEXT"),
                ("external_owner_pid", "INTEGER"),
                ("external_owner_token", "TEXT"),
                ("admission_owner_pid", "INTEGER"),
                ("cleanup", "INTEGER NOT NULL DEFAULT 0"),
                ("available_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in queue_columns:
                    conn.execute(f"ALTER TABLE queue ADD COLUMN {name} {declaration}")
            if "session_key" not in queue_columns:
                conn.execute("ALTER TABLE queue ADD COLUMN session_key TEXT NOT NULL DEFAULT '__legacy__'")
                conn.execute(
                    "UPDATE queue SET session_key=COALESCE("
                    "(SELECT session_id FROM export_jobs WHERE export_id=queue.task_id), '__legacy__') "
                    "WHERE mode='export'"
                )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(slot)").fetchall()}
            if "owner_token" not in columns:
                conn.execute("ALTER TABLE slot ADD COLUMN owner_token TEXT")
            export_columns = {row[1] for row in conn.execute("PRAGMA table_info(export_jobs)").fetchall()}
            if "legacy_inline" not in export_columns:
                conn.execute("ALTER TABLE export_jobs ADD COLUMN legacy_inline INTEGER NOT NULL DEFAULT 0")
            if "page_requests" not in export_columns:
                conn.execute("ALTER TABLE export_jobs ADD COLUMN page_requests INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS queue_session_state_seq ON queue(session_key,state,seq)")

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
        scheduling_session_id: str | None = None,
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

            session_key = self._session_key(scheduling_session_id or session_id)
            self._ensure_not_closing(conn, session_key, retry_after_seconds)
            busy = self._busy_info(conn, retry_after_seconds, session_key)
            if self._admission_blocked(conn, session_key, busy) and on_busy == "reject":
                raise SchedulerBusy(busy)
            if self._queue_length(conn) >= MAX_PENDING_QUEUE:
                raise SchedulerBusy(
                    BusyInfo(busy.active_task_id, busy.queue_length, retry_after_seconds, "CHATGPT_QUEUE_FULL")
                )
            seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM queue").fetchone()[0])
            now = time.time()
            conn.execute(
                "INSERT INTO queue(task_id,seq,state,mode,request_id,created_at,session_key) VALUES(?,?,?,?,NULL,?,?)",
                (export_id, seq, "QUEUED", "export", now, session_key),
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

    def _busy_info(
        self, conn: sqlite3.Connection, retry_after_seconds: int, session_key: str | None = None
    ) -> BusyInfo:
        session_key = session_key or self._legacy_key
        self._maintenance_busy(conn)
        maintenance = conn.execute("SELECT task_id FROM slot WHERE id=1").fetchone()
        lane = conn.execute("SELECT task_id FROM session_slots WHERE session_key=?", (session_key,)).fetchone()
        active = maintenance["task_id"] or (lane["task_id"] if lane else None)
        waiting = conn.execute(
            "SELECT 1 FROM queue WHERE session_key=? AND state IN ('ADMITTING','QUEUED') LIMIT 1", (session_key,)
        ).fetchone()
        committed = conn.execute(
            "SELECT COUNT(*) FROM (SELECT session_key FROM session_slots UNION "
            "SELECT session_key FROM queue WHERE state IN ('ADMITTING','QUEUED') AND cleanup=0)"
        ).fetchone()[0]
        full = lane is None and waiting is None and committed >= MAX_SESSION_SLOTS
        reason = "CHATGPT_BROWSER_BUSY" if maintenance["task_id"] else "SESSION_CAPACITY" if full else "SESSION_BUSY"
        return BusyInfo(active, self._queue_length(conn), retry_after_seconds, reason)

    @staticmethod
    def _admission_blocked(conn: sqlite3.Connection, session_key: str, busy: BusyInfo) -> bool:
        return bool(
            busy.active_task_id
            or busy.reason == "SESSION_CAPACITY"
            or conn.execute(
                "SELECT 1 FROM queue WHERE session_key=? AND state IN ('ADMITTING','QUEUED') LIMIT 1",
                (session_key,),
            ).fetchone()
        )

    def _ensure_not_closing(self, conn: sqlite3.Connection, session_key: str, retry_after_seconds: int) -> None:
        closing = conn.execute(
            "SELECT task_id FROM queue WHERE session_key=? AND cleanup=1 "
            "AND state IN ('ADMITTING','QUEUED','STARTING','RUNNING','CANCEL_REQUESTED') LIMIT 1",
            (session_key,),
        ).fetchone()
        if closing:
            raise SchedulerBusy(BusyInfo(str(closing[0]), self._queue_length(conn), retry_after_seconds, "SESSION_CLOSING"))

    def idempotent_task(self, request_id: str | None, request_hash: str | None) -> str | None:
        if not request_id:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT task_id,request_hash FROM queue WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise IdempotencyConflict("request_id was already used with different task inputs")
            return str(row["task_id"])

    def enqueue(
        self,
        task_id: str,
        mode: str,
        *,
        on_busy: str,
        retry_after_seconds: int,
        request_id: str | None = None,
        request_hash: str | None = None,
        cleanup: bool = False,
        session_id: str | None = None,
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
            session_key = self._session_key(session_id)
            if not cleanup:
                self._ensure_not_closing(conn, session_key, retry_after_seconds)
            busy = self._busy_info(conn, retry_after_seconds, session_key)
            if self._admission_blocked(conn, session_key, busy) and on_busy == "reject":
                raise SchedulerBusy(busy)
            if self._queue_length(conn) >= MAX_PENDING_QUEUE and not cleanup:
                raise SchedulerBusy(
                    BusyInfo(busy.active_task_id, busy.queue_length, retry_after_seconds, "CHATGPT_QUEUE_FULL")
                )
            seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM queue").fetchone()[0])
            conn.execute(
                "INSERT INTO queue(task_id,seq,state,mode,request_id,request_hash,created_at,session_key,admission_owner_pid,cleanup) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, seq, "ADMITTING", mode, request_id, request_hash, time.time(), session_key, os.getpid(), int(cleanup)),
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
        row = conn.execute("SELECT seq,state FROM queue WHERE task_id=?", (task_id,)).fetchone()
        if not row or row["state"] not in _WAITING_STATES:
            return 0
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM queue WHERE state IN ('ADMITTING','QUEUED') AND seq<=?", (row["seq"],)
            ).fetchone()[0]
        )

    def _maintenance_busy(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT * FROM slot WHERE id=1").fetchone()
        if not row or not row["task_id"]:
            return False
        if _pid_alive(row["owner_pid"]):
            return True
        queued = conn.execute("SELECT mode,state FROM queue WHERE task_id=?", (row["task_id"],)).fetchone()
        if queued and queued["mode"] != "browser_operation" and queued["state"] in (*_ACTIVE_STATES, "RECOVERY_REQUIRED"):
            # A pre-upgrade task used this row as its execution lease. Only
            # recover_startup, with access to its durable receipt, may recover it.
            return True
        if queued and queued["mode"] == "browser_operation":
            conn.execute("UPDATE queue SET state='CANCELLED' WHERE task_id=?", (row["task_id"],))
        conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1")
        return False

    def _reserve_lane(self, conn: sqlite3.Connection, row: sqlite3.Row, owner_pid: int, owner_token: str, state: str) -> bool:
        key = row["session_key"]
        if not row["cleanup"] and conn.execute(
            "SELECT 1 FROM queue WHERE session_key=? AND state='RECOVERY_REQUIRED' LIMIT 1", (key,),
        ).fetchone():
            return False
        lane = conn.execute("SELECT * FROM session_slots WHERE session_key=?", (key,)).fetchone()
        if lane and lane["task_id"]:
            return False
        if lane is None:
            if row["cleanup"]:
                # A closed Session without a resident lane must still be
                # cleanable when all four lanes are retained. Cleanup never
                # creates a page, and uses the exclusive gate instead of a fifth
                # lane. The queue key remains the target Session for page scope.
                if conn.execute("SELECT 1 FROM session_slots WHERE task_id IS NOT NULL LIMIT 1").fetchone():
                    return False
                conn.execute("UPDATE queue SET state=?,available_at=0 WHERE task_id=?", (state, row["task_id"]))
                conn.execute(
                    "UPDATE slot SET task_id=?,owner_pid=?,owner_token=?,state=?,heartbeat=? WHERE id=1",
                    (row["task_id"], owner_pid, owner_token, state, time.time()),
                )
                return True
            if conn.execute("SELECT COUNT(*) FROM session_slots").fetchone()[0] >= MAX_SESSION_SLOTS:
                return False
            conn.execute("INSERT INTO session_slots(session_key,persistent) VALUES(?,?)", (key, int(key != self._legacy_key)))
        conn.execute("UPDATE queue SET state=?,available_at=0 WHERE task_id=?", (state, row["task_id"]))
        conn.execute(
            "UPDATE session_slots SET task_id=?,owner_pid=?,owner_token=?,state=?,heartbeat=? WHERE session_key=?",
            (row["task_id"], owner_pid, owner_token, state, time.time(), key),
        )
        return True

    def reserve_next(
        self, owner_pid: int, owner_token: str, *, allow_exports: bool = True, exclude: set[str] | None = None
    ) -> str | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._maintenance_busy(conn):
                return None
            self._discard_abandoned_operations(conn)
            seen: set[str] = set()
            excluded = exclude or set()
            now = time.time()
            for row in conn.execute("SELECT * FROM queue WHERE state IN ('ADMITTING','QUEUED') ORDER BY seq").fetchall():
                key = row["session_key"]
                if key in seen:
                    continue
                seen.add(key)
                if row["state"] != "QUEUED" or row["available_at"] > now or row["task_id"] in excluded:
                    continue
                if row["mode"] == "browser_operation" or (row["mode"] == "export" and not allow_exports):
                    continue
                if self._reserve_lane(conn, row, owner_pid, owner_token, "STARTING"):
                    return str(row["task_id"])
            return None

    @staticmethod
    def _discard_abandoned_operations(conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT task_id,external_owner_pid FROM queue WHERE mode='browser_operation' "
                            "AND state IN ('QUEUED','STARTING','RUNNING','CANCEL_REQUESTED')").fetchall()
        for row in rows:
            if not _pid_alive(row["external_owner_pid"]):
                conn.execute("UPDATE queue SET state='CANCELLED' WHERE task_id=?", (row["task_id"],))

    def enqueue_browser_operation(
        self, task_id: str, owner_pid: int, owner_token: str, *, cleanup: bool = False,
        session_id: str | None = None, diagnostic: bool = False,
    ) -> bool:
        """Queue named work, or atomically admit an isolated diagnostic lease.

        A diagnostic must never borrow a resident Session: its upstream page
        can still be generating after a local TIMEOUT. It gets a temporary
        lane within the same four-slot budget, or an immediate retryable BUSY.
        """
        if diagnostic and (cleanup or session_id is not None):
            raise ValueError("A diagnostic cannot target or clean up a named Session")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if diagnostic:
                session_id = self._session_key(f"diagnostic-{task_id}")
                busy = self._busy_info(conn, 5, session_id)
                if self._admission_blocked(conn, session_id, busy):
                    raise SchedulerBusy(BusyInfo(busy.active_task_id, busy.queue_length, 5, "BROWSER_DIAGNOSTIC_BUSY"))
            elif session_id is None:
                rows = conn.execute("SELECT session_key FROM session_slots ORDER BY (task_id IS NOT NULL),session_key").fetchall()
                row = next((row for row in rows if self._owns_key(row[0])), None)
                session_id = str(row[0]) if row else self._legacy_key
            else:
                session_id = self._session_key(session_id)
            if not cleanup:
                self._ensure_not_closing(conn, session_id, 5)
            if self._queue_length(conn) >= MAX_PENDING_QUEUE and not cleanup:
                busy = self._busy_info(conn, 5, session_id)
                raise SchedulerBusy(BusyInfo(busy.active_task_id, busy.queue_length, 5, "CHATGPT_QUEUE_FULL"))
            seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM queue").fetchone()[0])
            conn.execute(
                "INSERT INTO queue(task_id,seq,state,mode,created_at,session_key,external_owner_pid,external_owner_token,cleanup) "
                "VALUES(?,?,'QUEUED','browser_operation',?,?,?,?,?)",
                (task_id, seq, time.time(), session_id, owner_pid, owner_token, int(cleanup)),
            )
            if diagnostic:
                row = conn.execute("SELECT * FROM queue WHERE task_id=?", (task_id,)).fetchone()
                if not self._reserve_lane(conn, row, owner_pid, owner_token, "RUNNING"):
                    raise SchedulerBusy(BusyInfo(None, self._queue_length(conn), 5, "BROWSER_DIAGNOSTIC_BUSY"))
                # Completion and restart recovery release this lease even if
                # the caller exits before JobManager's explicit release.
                conn.execute("UPDATE session_slots SET persistent=0 WHERE session_key=?", (session_id,))
                return True
            return False

    def reserve_browser_operation(self, task_id: str, owner_pid: int, owner_token: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._maintenance_busy(conn):
                return False
            self._discard_abandoned_operations(conn)
            row = conn.execute("SELECT * FROM queue WHERE task_id=?", (task_id,)).fetchone()
            if (
                not row or row["state"] != "QUEUED" or row["available_at"] > time.time()
                or row["external_owner_pid"] != owner_pid or row["external_owner_token"] != owner_token
            ):
                return False
            first = conn.execute("SELECT task_id FROM queue WHERE session_key=? AND state IN ('ADMITTING','QUEUED') ORDER BY seq LIMIT 1", (row["session_key"],)).fetchone()
            if not first or first[0] != task_id:
                return False
            return self._reserve_lane(conn, row, owner_pid, owner_token, "RUNNING")

    def operation_session(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT session_key FROM queue WHERE task_id=?", (task_id,)).fetchone()
            key = str(row[0]) if row else self._legacy_key
            return None if key == self._legacy_key else self._local_key(key)

    def session_active(self, session_id: str) -> str | None:
        session_id = self._session_key(session_id)
        with self._connect() as conn:
            row = conn.execute("SELECT task_id FROM session_slots WHERE session_key=?", (session_id,)).fetchone()
            if row and row[0]:
                return str(row[0])
            row = conn.execute(
                "SELECT slot.task_id FROM slot JOIN queue ON queue.task_id=slot.task_id WHERE queue.session_key=?",
                (session_id,),
            ).fetchone()
            return str(row[0]) if row else None

    def session_pending(self, session_id: str) -> bool:
        """Include work owned by other processes when checking Session expiry."""
        session_id = self._session_key(session_id)
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM queue WHERE session_key=? "
                "AND state IN ('ADMITTING','QUEUED','STARTING','RUNNING','CANCEL_REQUESTED','RECOVERY_REQUIRED') LIMIT 1",
                (session_id,),
            ).fetchone() is not None

    def pending_session_ids(self) -> set[str]:
        with self._connect() as conn:
            return {
                self._local_key(str(row[0])) for row in conn.execute(
                    "SELECT DISTINCT session_key FROM queue WHERE session_key<>? "
                    "AND state IN ('ADMITTING','QUEUED','STARTING','RUNNING','CANCEL_REQUESTED','RECOVERY_REQUIRED')",
                    (self._legacy_key,),
                )
            }

    def cancel_waiting_session_tasks(self, session_id: str) -> list[str]:
        """Cancel unsent jobs, including admission not yet published to JobStore."""
        session_id = self._session_key(session_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = [str(row[0]) for row in conn.execute(
                "SELECT task_id FROM queue WHERE session_key=? AND mode NOT IN ('export','browser_operation') "
                "AND state IN ('ADMITTING','QUEUED','RECOVERY_REQUIRED')", (session_id,),
            )]
            for task_id in ids:
                self._complete_conn(conn, task_id, "CANCELLED")
            return ids

    def release_session(self, session_id: str, *, after_task_id: str | None = None) -> bool:
        """Release an idle lane, or defer release until its exact cleanup ends.

        Passing the active cleanup ID avoids a gap between completion and lane
        deletion. Ordinary task owners cannot release another active operation.
        """
        session_id = self._session_key(session_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT task_id FROM session_slots WHERE session_key=?", (session_id,)).fetchone()
            if row and row[0]:
                if row[0] != after_task_id:
                    return False
                operation = conn.execute(
                    "SELECT 1 FROM queue WHERE task_id=? AND mode='browser_operation' AND cleanup=1", (after_task_id,)
                ).fetchone()
                if operation is None:
                    return False
                conn.execute("UPDATE session_slots SET persistent=0 WHERE session_key=?", (session_id,))
                return True
            conn.execute("DELETE FROM session_slots WHERE session_key=?", (session_id,))
            return True

    def set_state(self, task_id: str, state: str, *, owner_pid: int | None = None) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if owner_pid is not None and not conn.execute(
                "SELECT 1 FROM session_slots WHERE task_id=? AND owner_pid=? "
                "UNION ALL SELECT 1 FROM slot WHERE task_id=? AND owner_pid=?",
                (task_id, owner_pid, task_id, owner_pid),
            ).fetchone():
                return
            current = conn.execute("SELECT state,session_key FROM queue WHERE task_id=?", (task_id,)).fetchone()
            if current is None or current[0] == "CANCELLED":
                return
            if current[0] == "CANCEL_REQUESTED" and state in (*_WAITING_STATES, *_ACTIVE_STATES):
                state = "CANCEL_REQUESTED"
            conn.execute("UPDATE queue SET state=? WHERE task_id=?", (state, task_id))
            now = time.time()
            conn.execute("UPDATE session_slots SET state=?,heartbeat=? WHERE task_id=? AND session_key=?", (state, now, task_id, current["session_key"]))
            conn.execute("UPDATE slot SET state=?,heartbeat=? WHERE task_id=?", (state, now, task_id))

    def heartbeat(self, task_id: str, owner_token: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE session_slots SET heartbeat=? WHERE task_id=? AND owner_token=?", (time.time(), task_id, owner_token))
            conn.execute("UPDATE slot SET heartbeat=? WHERE id=1 AND task_id=? AND owner_token=?", (time.time(), task_id, owner_token))

    def acquire_external_slot(self, task_id: str, owner_pid: int, owner_token: str) -> bool:
        """Exclusive maintenance gate, not a normal session operation."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._maintenance_busy(conn) or self._queue_length(conn) or conn.execute("SELECT COUNT(*) FROM session_slots WHERE task_id IS NOT NULL").fetchone()[0]:
                return False
            conn.execute("UPDATE slot SET task_id=?,owner_pid=?,owner_token=?,state='FETCHING',heartbeat=? WHERE id=1", (task_id, owner_pid, owner_token, time.time()))
            return True

    def release_external_slot(self, task_id: str, owner_token: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1 AND task_id=? AND owner_token=?", (task_id, owner_token))

    def complete(self, task_id: str, state: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._complete_conn(conn, task_id, state)

    @staticmethod
    def _complete_conn(conn: sqlite3.Connection, task_id: str, state: str, *, available_at: float = 0) -> None:
        current = conn.execute("SELECT state,session_key FROM queue WHERE task_id=?", (task_id,)).fetchone()
        if current is None:
            return
        if state == "QUEUED" and current and current[0] in {"CANCELLED", "CANCEL_REQUESTED"}:
            state = "CANCELLED"
            available_at = 0
        conn.execute("UPDATE queue SET state=?,available_at=? WHERE task_id=?", (state, available_at, task_id))
        conn.execute("DELETE FROM session_slots WHERE task_id=? AND session_key=? AND persistent=0", (task_id, current["session_key"]))
        conn.execute(
            "UPDATE session_slots SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state='IDLE',heartbeat=NULL WHERE task_id=? AND session_key=?",
            (task_id, current["session_key"]),
        )
        conn.execute(
            "UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1 AND task_id=?",
            (task_id,),
        )

    def defer_capacity(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._complete_conn(conn, task_id, "QUEUED", available_at=time.time() + 0.5)

    def requeue_confirmed_poll(self, task_id: str) -> None:
        """A stopped worker has a durable receipt; resume polling, never sending.

        JobManager preserves RUNNING/CANCEL_REQUESTED in the Job envelope. This
        path differs from pre-send capacity retry: cancellation still needs a
        remote stop confirmation and must keep its place before later turns.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM queue WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row[0] == "CANCELLED":
                return
            conn.execute("UPDATE queue SET state='QUEUED' WHERE task_id=?", (task_id,))
            self._complete_conn(conn, task_id, "QUEUED")

    def request_cancel(self, task_id: str) -> str:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM queue WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return "MISSING"
            if row["state"] in {*_WAITING_STATES, "RECOVERY_REQUIRED"}:
                self._complete_conn(conn, task_id, "CANCELLED")
                return "CANCELLED"
            if row["state"] not in _ACTIVE_STATES:
                return str(row["state"])
            conn.execute("UPDATE queue SET state='CANCEL_REQUESTED' WHERE task_id=?", (task_id,))
            conn.execute("UPDATE session_slots SET state='CANCEL_REQUESTED',heartbeat=? WHERE task_id=?", (time.time(), task_id))
            conn.execute("UPDATE slot SET state='CANCEL_REQUESTED',heartbeat=? WHERE task_id=?", (time.time(), task_id))
            return "CANCEL_REQUESTED"

    def status(self, task_id: str | None = None) -> dict[str, object]:
        with self._connect() as conn:
            maintenance = conn.execute(
                "SELECT slot.task_id,slot.state,queue.session_key FROM slot LEFT JOIN queue ON queue.task_id=slot.task_id WHERE id=1"
            ).fetchone()
            slots = conn.execute("SELECT session_key,task_id,state,persistent FROM session_slots ORDER BY session_key").fetchall()
            active = [dict(row) for row in slots if row["task_id"]]
            if maintenance["task_id"]:
                active.append({**dict(maintenance), "persistent": 0})
            first = active[0] if active else None
            result: dict[str, object] = {
                "active_task_id": maintenance["task_id"] or (first["task_id"] if first else None),
                "active_state": maintenance["state"] if maintenance["task_id"] else first["state"] if first else None,
                "active_count": len(active), "active_tasks": active,
                "session_slots": [{**dict(row), "session_key": self._local_key(row["session_key"])}
                                  for row in slots if self._owns_key(row["session_key"])],
                "max_sessions": MAX_SESSION_SLOTS,
                "session_count": len(slots),
                "local_session_count": sum(self._owns_key(row["session_key"]) for row in slots),
                "pool_scope": "shared_profile" if self._pool_path is not None else "provider",
                "shared_session_slots": [dict(row) for row in slots],
                "queue_length": self._queue_length(conn),
            }
            if task_id:
                row = conn.execute("SELECT state,session_key FROM queue WHERE task_id=?", (task_id,)).fetchone()
                result["scheduler_state"] = row["state"] if row else None
                result["queue_position"] = self._queue_position_conn(conn, task_id) if row else None
                result["session_id"] = self._local_key(row["session_key"]) if row and row["session_key"] != self._legacy_key else None
            return result

    def recover_startup(self, store: object) -> None:
        """Recover each dead worker without replaying an unconfirmed send."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Backfill pre-upgrade queue envelopes from their durable jobs.
            get_job = getattr(store, "get", None)
            if get_job is not None:
                for row in conn.execute(
                    "SELECT task_id FROM queue WHERE session_key=? "
                    "AND state IN ('QUEUED','ADMITTING','RUNNING','STARTING','CANCEL_REQUESTED','RECOVERY_REQUIRED')",
                    (self._legacy_key,),
                ).fetchall():
                    job = get_job(row["task_id"])
                    if job and job.session_id:
                        conn.execute("UPDATE queue SET session_key=? WHERE task_id=?", (self._session_key(job.session_id), job.id))

            # Older services stored the current task in the maintenance row.
            # A live old service retains exclusive access until it exits.
            legacy = conn.execute("SELECT * FROM slot WHERE id=1").fetchone()
            if legacy["task_id"] and not _pid_alive(legacy["owner_pid"]):
                row = conn.execute("SELECT * FROM queue WHERE task_id=?", (legacy["task_id"],)).fetchone()
                if row:
                    self._recover_operation(conn, row, store)
                    key = row["session_key"]
                    if key != self._legacy_key and conn.execute("SELECT COUNT(*) FROM session_slots").fetchone()[0] < MAX_SESSION_SLOTS:
                        conn.execute(
                            "INSERT OR IGNORE INTO session_slots(session_key,persistent,state) VALUES(?,1,'IDLE')", (key,)
                        )
                conn.execute("UPDATE slot SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state=NULL,heartbeat=NULL WHERE id=1")

            for lane in conn.execute("SELECT * FROM session_slots WHERE task_id IS NOT NULL").fetchall():
                if not self._owns_key(lane["session_key"]):
                    continue
                if _pid_alive(lane["owner_pid"]):
                    continue
                task_id = lane["task_id"]
                row = conn.execute("SELECT * FROM queue WHERE task_id=?", (task_id,)).fetchone()
                if row:
                    self._recover_operation(conn, row, store)
                if lane["persistent"]:
                    conn.execute("UPDATE session_slots SET task_id=NULL,owner_pid=NULL,owner_token=NULL,state='IDLE',heartbeat=NULL WHERE session_key=?", (lane["session_key"],))
                else:
                    conn.execute("DELETE FROM session_slots WHERE session_key=?", (lane["session_key"],))

            # ADMITTING is published only after upload ownership and the Job
            # envelope have been committed. An incomplete admission must not
            # strand its Session's FIFO forever after its process exits.
            for row in conn.execute("SELECT * FROM queue WHERE state='ADMITTING'").fetchall():
                if not _pid_alive(row["admission_owner_pid"]):
                    self._recover_operation(conn, row, store)
            self._discard_abandoned_operations(conn)

    @staticmethod
    def _recover_operation(conn: sqlite3.Connection, row: sqlite3.Row, store: object) -> None:
        task_id = str(row["task_id"])
        state = str(row["state"])
        if row["mode"] == "browser_operation":
            state = "CANCELLED"
        elif row["mode"] == "export":
            record = conn.execute("SELECT state FROM export_jobs WHERE export_id=?", (task_id,)).fetchone()
            if record is None:
                state = "ERROR"
            elif state == "CANCEL_REQUESTED":
                state = "CANCELLED"
                conn.execute("UPDATE export_jobs SET state='CANCELLED',updated_at=? WHERE export_id=?", (time.time(), task_id))
            elif record[0] in {"READY", "FAILED", "CANCELLED", "GONE", "RELEASED"}:
                state = "COMPLETE" if record[0] in {"READY", "RELEASED"} else "CANCELLED" if record[0] == "CANCELLED" else "ERROR"
            elif state in {"STARTING", "RUNNING"}:
                state = "QUEUED"
                conn.execute("UPDATE export_jobs SET state='QUEUED',updated_at=? WHERE export_id=?", (time.time(), task_id))
                conn.execute(
                    "INSERT INTO export_events(export_id,created_at,event,metadata_json) VALUES(?,?,?,?)",
                    (task_id, time.time(), "REQUEUED_AFTER_RESTART", json.dumps({"recovery_reason": "worker_process_stopped"})),
                )
        else:
            get_job = getattr(store, "get", None)
            job = get_job(task_id) if get_job else None
            if job and job.state in {"COMPLETE", "ERROR", "TIMEOUT", "MODEL_UNAVAILABLE", "CANCELLED", "NEEDS_USER_INPUT"}:
                state = job.state
            elif state == "ADMITTING":
                state = "QUEUED" if job and job.state == "QUEUED" else "ERROR"
            elif job and job.state in {"RUNNING", "CANCEL_REQUESTED"} and job.conversation_url:
                # The durable receipt authorizes polling only. Keep the Job's
                # RUNNING/CANCEL_REQUESTED state so JobManager never resends it.
                state = "QUEUED"
            elif state in {*_ACTIVE_STATES, "RECOVERY_REQUIRED"}:
                state = "RECOVERY_REQUIRED"
                save_job = getattr(store, "save", None)
                if job and save_job:
                    from .models import utc_now

                    job.state = "ERROR"
                    job.error_code = "INTERRUPTED_BEFORE_RECEIPT"
                    job.error = "Worker stopped before the send could be confirmed; do not automatically resubmit."
                    job.progress = None
                    job.completed_at = utc_now()
                    save_job(job)
        conn.execute("UPDATE queue SET state=?,available_at=0 WHERE task_id=?", (state, task_id))
