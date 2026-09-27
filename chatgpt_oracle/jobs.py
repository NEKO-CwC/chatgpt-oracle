from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse
from typing import Any

from .browser import (
    BrowserNeedsUserAction,
    BrowserTaskError,
    ChatGPTBrowser,
    ModelUnavailableError,
    SubmitReceipt,
    StreamedAttachment,
)
from .config import Settings
from .models import Job, TERMINAL_STATES, public_job, utc_now
from .storage import JobStore
from .scheduler import (
    BusyInfo,
    IdempotencyConflict,
    Scheduler,
    SchedulerBusy,
)
from .uploads import UploadError, UploadStore
from .exports import (
    AttachmentUnavailable,
    ExportCapacityError,
    ExportSessionMismatch,
    ExportStore,
    ExportViewLease,
    InvalidExportCursor,
    MAX_PAGE_RESPONSE_BYTES,
    PageLimitTooSmall,
    make_export,
)
from .sessions import ConversationSession, SessionStore
from .file_store import FileStore, FileTransferError

logger = logging.getLogger("chatgpt_oracle.jobs")
QUALITY_DEFAULTS = {"consult": "extra_high", "critic": "extra_high", "research": "pro"}


class ExportRequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class JobManager:
    def __init__(
        self,
        settings: Settings,
        browser: ChatGPTBrowser | Any | None = None,
        uploads: UploadStore | None = None,
        *,
        export_owner: bool = True,
    ):
        self.settings = settings
        self.export_owner = export_owner
        self.store = JobStore(settings.jobs_dir)
        self.scheduler = Scheduler(
            settings.jobs_dir,
            export_audit_retention_seconds=settings.export_audit_retention_seconds,
        )
        self.browser = browser or ChatGPTBrowser(settings)
        self.uploads = uploads or UploadStore(settings)
        self.files = FileStore(
            settings.file_store_dir or settings.jobs_dir / "file-store",
            max_file_bytes=settings.file_store_max_file_bytes,
            max_total_bytes=settings.file_store_max_total_bytes,
            max_transfers=settings.file_store_max_transfers,
            max_chunk_bytes=settings.file_store_max_chunk_bytes,
            io_chunk_bytes=settings.file_store_io_chunk_bytes,
            ttl_seconds=settings.file_store_ttl_seconds,
            metadata_retention_seconds=settings.file_store_metadata_retention_seconds,
        )
        self._file_download_tasks: dict[str, asyncio.Task[None]] = {}
        self._file_download_owner_tokens: dict[str, str] = {}
        self._browser_file_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._file_store_start_lock = asyncio.Lock()
        self._file_store_recovered = False
        self._receipts: dict[str, SubmitReceipt] = {}
        self._owner_token = uuid.uuid4().hex
        self.exports = ExportStore(
            ttl_seconds=settings.export_ttl_seconds,
            max_lifetime_seconds=settings.session_ttl_seconds,
            max_snapshot_bytes=settings.export_max_snapshot_bytes,
            snapshot_dir=settings.jobs_dir / "export-snapshots",
        )
        self._export_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._active_export_tasks: dict[str, asyncio.Task[None]] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._export_startup_reconciled = False
        self._active_session_ids: set[str] = set()
        sessions_dir = settings.sessions_dir or (settings.root / "runtime" / "sessions")
        self.sessions = SessionStore(sessions_dir, ttl_seconds=settings.session_ttl_seconds)

    async def start(self) -> None:
        self._stopping = False
        self.settings.prepare_dirs()
        self.scheduler.recover_startup(self.store)
        if not self._file_store_recovered:
            async with self._file_store_start_lock:
                if not self._file_store_recovered:
                    active_ids = {
                        job.id
                        for job in self.store.all()
                        if job.state
                        in {"QUEUED", "STARTING", "UPLOADING", "ATTACHMENTS_VERIFIED", "SEND_PENDING", "RUNNING", "CANCEL_REQUESTED"}
                    }
                    await asyncio.to_thread(self.files.recover, active_ids, recover_downloads=True)
                    self._file_store_recovered = True
        async with self._lock:
            if self._worker is None or self._worker.done():
                if self.export_owner and not self._export_startup_reconciled:
                    self._reconcile_persisted_ready_exports()
                    self._run_export_maintenance()
                    self._export_startup_reconciled = True
                self._worker = asyncio.create_task(self._worker_loop(), name="chatgpt-oracle-worker")
            if self.export_owner and (self._maintenance_task is None or self._maintenance_task.done()):
                self._maintenance_task = asyncio.create_task(
                    self._export_maintenance_loop(),
                    name="chatgpt-oracle-export-maintenance",
                )
        self._wake.set()

    def _reconcile_persisted_ready_exports(self) -> None:
        for record in self.scheduler.list_exports(states=("READY",)):
            export_id = str(record["export_id"])
            try:
                self.exports.snapshot_status(str(record["session_id"]), export_id)
            except KeyError:
                self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
                self.scheduler.record_export_event(export_id, "GONE", {"reason": "snapshot_not_retained"})

    async def stop(self) -> None:
        self._stopping = True
        active_file_downloads = list(self._file_download_tasks.values())
        for task in active_file_downloads:
            task.cancel()
        if active_file_downloads:
            await asyncio.gather(*active_file_downloads, return_exceptions=True)
        active_exports = list(self._active_export_tasks.values())
        if self._worker:
            self._worker.cancel()
        for task in active_exports:
            task.cancel()
        if active_exports:
            await asyncio.gather(*active_exports, return_exceptions=True)
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        if self._worker:
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        self._active_session_ids.clear()
        for job in self.store.all():
            await self.uploads.release_job(job.id)
        for future in self._export_waiters.values():
            if not future.done():
                future.set_exception(RuntimeError("export manager stopped"))
        self._export_waiters.clear()
        await self.browser.close()
        self.exports.close()

    def _log_pruned_exports(self, counts: dict[str, int]) -> None:
        if any(counts.values()):
            logger.info(
                "export history pruned jobs=%d events=%d queue_rows=%d retention_seconds=%d",
                counts["jobs"],
                counts["events"],
                counts["queue_rows"],
                int(self.settings.export_audit_retention_seconds),
            )

    def _reconcile_expired_snapshots(self, *, now: float | None = None) -> None:
        for export_id in self.exports.pop_expired_snapshots(now=now):
            record = self.scheduler.get_export(export_id)
            if not record or record["state"] != "READY":
                continue
            self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
            self.scheduler.record_export_event(export_id, "GONE", {"reason": "snapshot_ttl"})

    def _run_export_maintenance(self, *, now: float | None = None) -> None:
        self._reconcile_expired_snapshots(now=now)
        self._log_pruned_exports(self.scheduler.prune_export_history(now=now))

    async def _export_maintenance_loop(self) -> None:
        interval = max(
            0.05,
            min(
                30.0,
                self.settings.export_ttl_seconds / 4,
                self.settings.export_audit_retention_seconds / 4,
            ),
        )
        while True:
            await asyncio.sleep(interval)
            self._run_export_maintenance()
            await asyncio.to_thread(self.files.recover, None)

    @staticmethod
    def _task_request_hash(job: Job) -> str:
        inputs = {
            "mode": job.mode,
            "prompt": job.prompt,
            "project": job.project,
            "model": job.model,
            "quality": job.quality,
            "reasoning_effort": job.reasoning_effort,
            "allow_downgrade": job.allow_downgrade,
            "session_id": job.session_id,
            "upload_ids": job.upload_ids,
            "file_refs": job.file_refs,
        }
        return hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    async def submit(
        self,
        mode: str,
        prompt: str,
        *,
        project: str | None = None,
        model: str | None = None,
        quality: str = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        on_busy: str = "reject",
        request_id: str | None = None,
        upload_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
        session_id: str | None = None,
    ) -> Job:
        if mode not in {"consult", "research", "critic"}:
            raise ValueError("mode must be consult, research, or critic")
        if quality not in {"auto", "extra_high", "pro"}:
            raise ValueError("quality must be auto, extra_high, or pro")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if len(prompt) > 100_000:
            raise ValueError("prompt is too large; limit is 100000 characters")
        await self.start()
        upload_ids = list(upload_ids or [])
        file_refs = list(file_refs or [])
        if upload_ids and file_refs:
            raise ValueError("use upload_ids or file_refs, not both")
        if session_id:
            existing_session = self.sessions.get(session_id)
            if existing_session and existing_session.conversation_url:
                raise ValueError("session_id is already bound; use chatgpt_resume for an existing session")
            if existing_session and existing_session.last_task_id:
                previous = self.store.get(existing_session.last_task_id)
                if previous and previous.state not in TERMINAL_STATES:
                    raise ValueError(f"session is still processing task: {previous.id}")
            session_id = self.sessions.validate_id(session_id)
        elif file_refs:
            raise ValueError("file_refs require a named session")
        if file_refs and session_id and self.sessions.get(session_id) is None:
            raise FileTransferError("SESSION_NOT_FOUND", "named session not found", 404)
        job = Job.create(
            mode,
            prompt,
            project=project,
            model=model,
            quality=quality,
            reasoning_effort=reasoning_effort,
            allow_downgrade=allow_downgrade,
            session_id=session_id,
            file_refs=file_refs,
        )
        retry_after = 180 if (quality == "pro" or (quality == "auto" and mode == "research")) else 30
        admission = self.scheduler.enqueue(
            job.id,
            mode,
            on_busy=on_busy,
            retry_after_seconds=retry_after,
            request_id=request_id,
            request_hash=self._task_request_hash(job),
        )
        if admission.get("idempotent"):
            existing = self.store.get(str(admission["task_id"]))
            if existing is None:
                raise SchedulerBusy(BusyInfo(str(admission["task_id"]), 0, 1, "REQUEST_IN_PROGRESS"))
            return existing
        try:
            attached = await self.uploads.attach(upload_ids, job.id)
            file_records = self.files.attach_files_to_task(session_id, job.id, file_refs) if file_refs else []
        except Exception:
            await self.uploads.release_job(job.id)
            self.scheduler.complete(job.id, "ERROR")
            raise
        job.upload_ids = upload_ids
        job.attachments = [item.metadata.public() for item in attached] + [record.public() for record in file_records]
        try:
            if session_id:
                self.sessions.touch(session_id, last_task_id=job.id)
            self.store.save(job)
            self.scheduler.set_state(job.id, "QUEUED")
        except Exception:
            await self.uploads.release_job(job.id)
            self.files.release_task_files(job.id)
            self.scheduler.complete(job.id, "ERROR")
            raise
        self._wake.set()
        return job

    async def continue_task(
        self,
        task_id: str,
        prompt: str,
        *,
        quality: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        allow_downgrade: bool | None = None,
        on_busy: str = "reject",
        request_id: str | None = None,
        upload_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
    ) -> Job:
        parent = self.store.get(task_id)
        if not parent:
            raise KeyError(f"task not found: {task_id}")
        if not parent.conversation_url:
            raise ValueError("task has no conversation URL to continue")
        if parent.state not in {"COMPLETE", "NEEDS_USER_INPUT"}:
            raise ValueError(f"task is not ready for continuation: {parent.state}")
        await self.start()
        upload_ids = list(upload_ids or [])
        file_refs = list(file_refs or [])
        if upload_ids and file_refs:
            raise ValueError("use upload_ids or file_refs, not both")
        if file_refs and not parent.session_id:
            raise ValueError("file_refs require a named session")
        job = Job.create(
            parent.mode,
            prompt,
            project=parent.project,
            model=parent.model if model is None else model,
            quality=parent.quality if quality is None else quality,
            reasoning_effort=parent.reasoning_effort if reasoning_effort is None else reasoning_effort,
            allow_downgrade=parent.allow_downgrade if allow_downgrade is None else allow_downgrade,
            parent_id=parent.id,
            session_id=parent.session_id,
            file_refs=file_refs,
        )
        job.conversation_url = parent.conversation_url
        retry_after = 180 if (job.quality == "pro" or (job.quality == "auto" and job.mode == "research")) else 30
        admission = self.scheduler.enqueue(
            job.id,
            job.mode,
            on_busy=on_busy,
            retry_after_seconds=retry_after,
            request_id=request_id,
            request_hash=self._task_request_hash(job),
        )
        if admission.get("idempotent"):
            existing = self.store.get(str(admission["task_id"]))
            if existing is None:
                raise SchedulerBusy(BusyInfo(str(admission["task_id"]), 0, 1, "REQUEST_IN_PROGRESS"))
            return existing
        try:
            attached = await self.uploads.attach(upload_ids, job.id)
            file_records = self.files.attach_files_to_task(job.session_id, job.id, file_refs) if file_refs and job.session_id else []
        except Exception:
            await self.uploads.release_job(job.id)
            self.scheduler.complete(job.id, "ERROR")
            raise
        job.upload_ids = upload_ids
        job.attachments = [item.metadata.public() for item in attached] + [record.public() for record in file_records]
        try:
            if job.session_id:
                self.sessions.touch(job.session_id, conversation_url=job.conversation_url, last_task_id=job.id)
            self.store.save(job)
            self.scheduler.set_state(job.id, "QUEUED")
        except Exception:
            await self.uploads.release_job(job.id)
            self.files.release_task_files(job.id)
            self.scheduler.complete(job.id, "ERROR")
            raise
        self._wake.set()
        return job

    def get(self, task_id: str) -> Job:
        job = self.store.get(task_id)
        if not job:
            raise KeyError(f"task not found: {task_id}")
        return job

    def result(self, task_id: str) -> tuple[Job, str | None]:
        job = self.get(task_id)
        return job, self.store.read_result(job)

    def result_reference(self, task_id: str) -> dict[str, Any]:
        job = self.get(task_id)
        if job.state != "COMPLETE":
            return {"result_id": job.id, "ready": False, "state": job.state}
        page = self.store.read_result_page(job, cursor=None, max_response_bytes=512)
        return {key: value for key, value in page.items() if key != "text"} | {"ready": True}

    def result_page(
        self,
        session_id: str,
        result_id: str,
        *,
        cursor: str | None,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        job = self.get(result_id)
        if job.session_id != session_id:
            raise FileTransferError("RESULT_SESSION_MISMATCH", "result does not belong to this session", 404)
        if job.state != "COMPLETE":
            raise FileTransferError("RESULT_NOT_READY", "task result is not complete", 409)
        try:
            return self.store.read_result_page(
                job, cursor=cursor, max_response_bytes=max_response_bytes
            )
        except FileNotFoundError as exc:
            raise FileTransferError("RESULT_GONE", "result file is unavailable", 410) from exc
        except ValueError as exc:
            raise FileTransferError("INVALID_RESULT_PAGE", str(exc)) from exc

    @staticmethod
    def _canonical_conversation_url(value: str) -> str:
        parsed = urlparse(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"}
            or not parsed.path.startswith("/c/")
            or len(parsed.path.rstrip("/").split("/")) != 3
        ):
            raise ExportRequestError("INVALID_CONVERSATION_URL", "conversation_url must identify one ChatGPT conversation")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

    @staticmethod
    def _url_session_id(conversation_url: str) -> str:
        return "url-" + hashlib.sha256(conversation_url.encode("utf-8")).hexdigest()[:24]

    def _resolve_export_session(
        self,
        *,
        task_id: str | None,
        session_id: str | None,
        conversation_url: str | None,
    ) -> tuple[str, str]:
        if session_id and task_id:
            raise ExportRequestError("SOURCE_CONFLICT", "provide session_id or task_id, not both")
        if task_id and conversation_url:
            raise ExportRequestError("SOURCE_CONFLICT", "provide task_id or conversation_url, not both")

        if session_id:
            session = self.sessions.get(session_id)
            if not session:
                raise KeyError(f"session not found: {session_id}")
            if conversation_url:
                normalized = self._canonical_conversation_url(conversation_url)
                if session.conversation_url and session.conversation_url != normalized:
                    raise ExportRequestError("SESSION_URL_MISMATCH", "session is already bound to a different conversation")
                self.sessions.touch(session.id, conversation_url=normalized)
                return session.id, normalized
            if not session.conversation_url:
                raise ExportRequestError("SESSION_UNBOUND", "session has no conversation URL")
            normalized = self._canonical_conversation_url(session.conversation_url)
            self.sessions.touch(session.id, conversation_url=normalized)
            return session.id, normalized

        if task_id:
            job = self.store.get(task_id)
            if not job:
                raise KeyError(f"task not found: {task_id}")
            if not job.conversation_url:
                raise ExportRequestError("SESSION_UNBOUND", "task has no conversation URL")
            normalized = self._canonical_conversation_url(job.conversation_url)
            resolved_session_id = job.session_id or self._url_session_id(normalized)
            session = self.sessions.get(resolved_session_id) or self.sessions.create(resolved_session_id)
            if session.conversation_url and session.conversation_url != normalized:
                raise ExportRequestError("SESSION_URL_MISMATCH", "task session is bound to a different conversation")
            self.sessions.touch(resolved_session_id, conversation_url=normalized, last_task_id=job.id)
            return resolved_session_id, normalized

        if not conversation_url:
            raise ExportRequestError("SOURCE_REQUIRED", "session_id, task_id, or conversation_url is required")
        normalized = self._canonical_conversation_url(conversation_url)
        resolved_session_id = self._url_session_id(normalized)
        existing = self.sessions.get(resolved_session_id)
        if existing and existing.conversation_url and existing.conversation_url != normalized:
            raise ExportRequestError("SESSION_URL_MISMATCH", "generated URL session is bound to a different conversation")
        self.sessions.create(resolved_session_id)
        self.sessions.touch(resolved_session_id, conversation_url=normalized)
        return resolved_session_id, normalized

    def _export_receipt(self, record: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "accepted": record["state"] in {"QUEUED", "RUNNING", "CANCEL_REQUESTED", "READY"},
            "session_id": record["session_id"],
            "export_id": record["export_id"],
            "state": record["state"],
            "scope": record["scope"],
            "source": record.get("source"),
            "completeness": record.get("completeness"),
            "reason": record.get("reason"),
            "message_count": record.get("message_count"),
            "attachment_count": record.get("attachment_count"),
            "page_requests": int(record.get("page_requests") or 0),
            "views": record.get("views"),
            "error_code": record.get("error_code"),
            "expires_at": record.get("expires_at"),
        }
        if record["state"] == "NEEDS_USER_INPUT":
            result["instructions"] = record.get("error_detail")
        return result

    @staticmethod
    def _require_ready(status: dict[str, Any]) -> None:
        state = str(status.get("state"))
        if state == "READY":
            return
        if state == "GONE":
            raise ExportRequestError("EXPORT_GONE", "export snapshot expired or was lost after restart")
        if state == "RELEASED":
            raise ExportRequestError("EXPORT_RELEASED", "export snapshot has been released")
        if state == "NEEDS_USER_INPUT":
            raise ExportRequestError("NEEDS_USER_INPUT", "manual browser action is required")
        if state == "FAILED":
            raise ExportRequestError(str(status.get("error_code") or "EXPORT_FAILED"), "export failed")
        raise ExportRequestError("EXPORT_NOT_READY", f"export is {state}")

    async def export_start(
        self,
        *,
        task_id: str | None = None,
        conversation_url: str | None = None,
        session_id: str | None = None,
        scope: str = "all",
        count: int = 10,
        on_busy: str = "reject",
        request_id: str | None = None,
        legacy_inline: bool = False,
    ) -> dict[str, Any]:
        if scope not in {"recent", "all"}:
            raise ValueError("scope must be recent or all")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0 or count > self.settings.export_max_count:
            raise ValueError(f"count must be between 1 and {self.settings.export_max_count}")
        resolved_session_id, normalized_url = self._resolve_export_session(
            task_id=task_id,
            session_id=session_id,
            conversation_url=conversation_url,
        )
        normalized_inputs = json.dumps(
            [resolved_session_id, normalized_url, scope, count, bool(legacy_inline)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(normalized_inputs.encode("utf-8")).hexdigest()
        request_key = (
            hashlib.sha256(f"export\0{resolved_session_id}\0{request_id}".encode("utf-8")).hexdigest()
            if request_id
            else None
        )
        await self.start()
        export_id = f"exp_{uuid.uuid4().hex[:20]}"
        try:
            admission = self.scheduler.enqueue_export(
                export_id,
                session_id=resolved_session_id,
                conversation_url=normalized_url,
                scope=scope,
                count=count,
                legacy_inline=legacy_inline,
                request_key=request_key,
                input_fingerprint=fingerprint,
                on_busy=on_busy,
                retry_after_seconds=180 if scope == "all" else 30,
            )
        except IdempotencyConflict as exc:
            raise ExportRequestError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
        export_id = str(admission["task_id"])
        record = self.scheduler.get_export(export_id)
        if record is None:
            raise RuntimeError("export admission did not persist its request envelope")
        if record["state"] == "READY":
            try:
                self.exports.snapshot_status(resolved_session_id, export_id)
            except KeyError:
                self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
                record = self.scheduler.get_export(export_id) or record
        self.scheduler.record_export_event(
            export_id,
            "ACCEPTED" if not admission.get("idempotent") else "IDEMPOTENT_REPLAY",
            {"operation": scope},
        )
        if not admission.get("idempotent"):
            self._wake.set()
        return {**self._export_receipt(record), "idempotent": bool(admission.get("idempotent")), **{
            key: admission[key] for key in ("queue_position", "queued_behind") if key in admission
        }}

    async def export_status(self, session_id: str, export_id: str) -> dict[str, Any]:
        self._reconcile_expired_snapshots()
        record = self.scheduler.get_export(export_id)
        if record is None:
            raise KeyError(f"export not found: {export_id}")
        if record["session_id"] != session_id:
            raise ExportSessionMismatch("export belongs to a different session")
        if record["state"] == "READY":
            try:
                snapshot_status = self.exports.snapshot_status(session_id, export_id)
            except KeyError:
                self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
                self.scheduler.record_export_event(export_id, "GONE", {"reason": "snapshot_not_retained"})
                record = self.scheduler.get_export(export_id) or record
            else:
                return {**self._export_receipt(record), **snapshot_status}
        response = self._export_receipt(record)
        scheduler_status = self.scheduler.status(export_id)
        if record["state"] in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
            queue_position = int(scheduler_status.get("queue_position") or 0)
            response["queue_position"] = queue_position
            response["retry_after_seconds"] = 5 if record["state"] != "QUEUED" else max(5, min(30, queue_position * 5))
        return response

    async def export_page(
        self,
        *,
        session_id: str,
        export_id: str,
        view: str = "messages",
        cursor: str | None,
        max_response_bytes: int = MAX_PAGE_RESPONSE_BYTES,
    ) -> dict[str, Any]:
        status = await self.export_status(session_id, export_id)
        self._require_ready(status)
        session = self.sessions.get(session_id)
        if not session or session.conversation_url != self.scheduler.get_export(export_id)["conversation_url"]:
            raise ExportRequestError("EXPORT_SESSION_MISMATCH", "session URL no longer matches the export snapshot")
        page_requests = self.scheduler.record_export_page_request(export_id)
        try:
            page = self.exports.page(
                session_id=session_id,
                export_id=export_id,
                view=view,
                cursor=cursor,
                max_response_bytes=max_response_bytes,
            )
        except KeyError as exc:
            self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
            self.scheduler.record_export_event(export_id, "GONE", {"reason": "snapshot_not_retained"})
            raise ExportRequestError("EXPORT_GONE", "export snapshot expired or was lost") from exc
        expiry = self._expiry_timestamp(page.get("expires_at"))
        if expiry is not None:
            self.scheduler.update_export(export_id, expires_at=expiry)
        if page["eof"]:
            self.scheduler.record_export_event(
                export_id,
                "VIEW_EOF",
                {
                    "view": view,
                    "bytes": page["total_bytes"],
                    "sha256": page["sha256"],
                    "page_request_count": page_requests,
                    "eof": True,
                    **(
                        {
                            "byte_offset": page["byte_offset"],
                            "returned_bytes": page["returned_bytes"],
                        }
                        if "byte_offset" in page
                        else {
                            "message_offset": page["start_ordinal"],
                            "returned_count": len(page["messages"]) + int(page["message_segment"] is not None),
                            "message_segment": page["message_segment"] is not None,
                        }
                    ),
                },
            )
        return page

    @staticmethod
    def _expiry_timestamp(value: object) -> float | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    async def export_attachment_handle(
        self,
        *,
        session_id: str,
        export_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:
        status = await self.export_status(session_id, export_id)
        self._require_ready(status)
        try:
            handle = self.exports.attachment_handle(session_id, export_id, attachment_id)
        except KeyError as exc:
            self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
            self.scheduler.record_export_event(export_id, "GONE", {"reason": "snapshot_not_retained"})
            raise ExportRequestError("EXPORT_GONE", "export snapshot expired or was lost") from exc
        snapshot = self.exports.snapshot_status(session_id, export_id)
        expiry = self._expiry_timestamp(snapshot.get("expires_at"))
        if expiry is not None:
            self.scheduler.update_export(export_id, expires_at=expiry)
        self.scheduler.record_export_event(
            export_id,
            "ATTACHMENT_HANDLE_ISSUED",
            {"operation": "attachment_handle"},
        )
        return {
            "session_id": session_id,
            "export_id": export_id,
            "attachment_id": attachment_id,
            "download_url": handle.public_url(self.exports.url_prefix),
            "expires_at": datetime.fromtimestamp(handle.expires_at, timezone.utc).isoformat(),
        }

    async def start_attachment_download(
        self,
        *,
        session_id: str,
        export_id: str,
        attachment_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        status = await self.export_status(session_id, export_id)
        self._require_ready(status)
        try:
            attachment, source_url, conversation_url = self.exports.attachment_source(
                session_id, export_id, attachment_id
            )
        except KeyError as exc:
            raise ExportRequestError("EXPORT_GONE", "export snapshot expired or was lost") from exc
        source_identity = f"{export_id}\0{attachment_id}"
        transfer = self.files.create_download(
            session_id=session_id,
            filename=str(attachment.get("filename") or "attachment.bin"),
            mime_type=attachment.get("mime_type"),
            source_identity=source_identity,
            file_kind=str(attachment.get("file_kind") or "unknown"),
            request_id=request_id,
        )
        transfer_id = str(transfer["transfer_id"])
        if transfer["state"] != "FETCHING":
            return transfer
        if transfer.get("idempotent"):
            if transfer_id in self._file_download_tasks:
                return transfer
            return transfer
        owner_token = f"file-{uuid.uuid4().hex}"
        if not self.scheduler.acquire_external_slot(transfer_id, os.getpid(), owner_token):
            self.files.rollback_unstarted_download(transfer_id, session_id)
            busy = self.scheduler.status()
            raise SchedulerBusy(
                BusyInfo(
                    str(busy.get("active_task_id")) if busy.get("active_task_id") else None,
                    int(busy.get("queue_length") or 0),
                    30,
                    "CHATGPT_BROWSER_BUSY",
                )
            )
        self._file_download_owner_tokens[transfer_id] = owner_token
        task = asyncio.create_task(
            self._materialize_attachment(
                session_id,
                export_id,
                attachment_id,
                source_url,
                conversation_url,
                transfer_id,
                owner_token,
            ),
            name=f"chatgpt-file-download-{transfer_id}",
        )
        self._file_download_tasks[transfer_id] = task
        return transfer

    async def _materialize_attachment(
        self,
        session_id: str,
        export_id: str,
        attachment_id: str,
        source_url: str,
        conversation_url: str,
        transfer_id: str,
        owner_token: str,
    ) -> None:
        try:
            destination = self.files.download_path(transfer_id, session_id)
            async with self._browser_file_lock:
                result = await self.browser.download_attachment(
                    source_url,
                    attachment_id=attachment_id,
                    conversation_url=conversation_url,
                    destination=destination,
                    max_file_bytes=self.settings.file_store_max_file_bytes,
                    on_progress=lambda offset: asyncio.to_thread(
                        self._record_file_download_progress,
                        transfer_id,
                        session_id,
                        owner_token,
                        offset,
                    ),
                )
            if not isinstance(result, StreamedAttachment):
                raise BrowserTaskError("ChatGPT file path did not use the streaming adapter")
            await asyncio.to_thread(
                self._record_file_download_progress,
                transfer_id,
                session_id,
                owner_token,
                result.size,
            )
            ready = await asyncio.to_thread(
                self.files.complete_download, transfer_id, session_id, result.mime_type
            )
            record = self.files.get_file(session_id, str(ready["file_ref"]))
            self.scheduler.record_export_event(
                export_id,
                "ATTACHMENT_DOWNLOADED",
                {
                    "operation": "file_download",
                    "bytes": record.size,
                    "sha256": record.sha256,
                    "file_kind": record.representation,
                },
            )
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(self.files.cancel_transfer, session_id, transfer_id)
            except FileTransferError:
                pass
            raise
        except Exception as exc:
            try:
                await asyncio.to_thread(
                    self.files.fail_download,
                    session_id,
                    transfer_id,
                    f"SOURCE_{type(exc).__name__.upper()}",
                )
            except FileTransferError:
                pass
            self.scheduler.record_export_event(
                export_id,
                "ATTACHMENT_DOWNLOAD_FAILED",
                {"operation": "file_download", "error_type": type(exc).__name__},
            )
        finally:
            self.scheduler.release_external_slot(transfer_id, owner_token)
            self._file_download_tasks.pop(transfer_id, None)
            self._file_download_owner_tokens.pop(transfer_id, None)

    def _record_file_download_progress(
        self, transfer_id: str, session_id: str, owner_token: str, offset: int
    ) -> None:
        self.files.update_download_progress(transfer_id, session_id, offset)
        self.scheduler.heartbeat(transfer_id, owner_token)

    async def export_file(
        self,
        *,
        session_id: str,
        export_id: str,
        view: str,
    ) -> tuple[ExportViewLease, str, str]:
        status = await self.export_status(session_id, export_id)
        self._require_ready(status)
        session = self.sessions.get(session_id)
        record = self.scheduler.get_export(export_id)
        if not session or not record or session.conversation_url != record["conversation_url"]:
            raise ExportRequestError("EXPORT_SESSION_MISMATCH", "session URL no longer matches the export snapshot")
        try:
            snapshot = self.exports.snapshot_status(session_id, export_id)
        except (KeyError, OSError) as exc:
            self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
            self.scheduler.record_export_event(export_id, "GONE", {"reason": "snapshot_not_retained"})
            raise ExportRequestError("EXPORT_GONE", "export snapshot expired or was lost") from exc
        expiry = self._expiry_timestamp(snapshot.get("expires_at"))
        if expiry is not None:
            self.scheduler.update_export(export_id, expires_at=expiry)
        lease = self.exports.view_file(session_id, export_id, view)
        try:
            self.scheduler.record_export_event(
                export_id,
                "VIEW_FILE_STARTED",
                {
                    "view": view,
                    "bytes": lease.size,
                },
            )
        except Exception:
            self.exports.release_view_file(lease)
            raise
        extension, content_type = {
            "messages": ("json", "application/json; charset=utf-8"),
            "attachments": ("json", "application/json; charset=utf-8"),
            "markdown": ("md", "text/markdown; charset=utf-8"),
        }[view]
        return lease, content_type, f"chatgpt-export-{export_id}-{view}.{extension}"

    def export_file_finished(
        self,
        lease: ExportViewLease,
        *,
        sent_bytes: int,
        error_type: str | None = None,
    ) -> None:
        complete = error_type is None and sent_bytes == lease.size
        metadata: dict[str, object] = {"view": lease.view, "bytes": sent_bytes}
        event = "VIEW_FILE_SERVED" if complete else "VIEW_FILE_FAILED"
        if complete:
            metadata["sha256"] = lease.sha256
        elif error_type:
            metadata["error_type"] = error_type
        try:
            self.scheduler.record_export_event(lease.snapshot.export_id, event, metadata)
        finally:
            self.exports.release_view_file(lease)

    async def export_release(self, session_id: str, export_id: str) -> dict[str, Any]:
        self._reconcile_expired_snapshots()
        record = self.scheduler.get_export(export_id)
        if record is None:
            raise KeyError(f"export not found: {export_id}")
        if record["session_id"] != session_id:
            raise ExportSessionMismatch("export belongs to a different session")
        state = str(record["state"])
        if state in {"RELEASED", "CANCELLED", "GONE", "FAILED", "NEEDS_USER_INPUT"}:
            return self._export_receipt(record)
        if state in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
            raise ExportRequestError(
                "EXPORT_NOT_READY",
                "queued or running exports must be cancelled with chatgpt_export_cancel",
            )
        try:
            self.exports.release_snapshot(session_id, export_id)
        except KeyError:
            self.scheduler.update_export(export_id, state="GONE", error_code="EXPORT_GONE")
            self.scheduler.record_export_event(export_id, "GONE", {"reason": "snapshot_not_retained"})
            return self._export_receipt(self.scheduler.get_export(export_id) or record)
        self.scheduler.update_export(export_id, state="RELEASED")
        self.scheduler.record_export_event(export_id, "RELEASED")
        return self._export_receipt(self.scheduler.get_export(export_id) or record)

    async def export_cancel(self, session_id: str, export_id: str) -> dict[str, Any]:
        record = self.scheduler.get_export(export_id)
        if record is None:
            raise KeyError(f"export not found: {export_id}")
        if record["session_id"] != session_id:
            raise ExportSessionMismatch("export belongs to a different session")
        if record["state"] in {"CANCELLED", "RELEASED", "GONE", "FAILED", "NEEDS_USER_INPUT"}:
            return self._export_receipt(record)
        if record["state"] == "READY":
            raise ExportRequestError("EXPORT_ALREADY_READY", "READY exports are released with chatgpt_export_release")

        cancellation = self.scheduler.request_cancel(export_id)
        if cancellation == "MISSING":
            raise KeyError(f"export not found: {export_id}")
        if cancellation == "CANCELLED":
            self.scheduler.update_export(export_id, state="CANCELLED", error_code="CANCELLED")
            self.scheduler.record_export_event(export_id, "CANCELLED", {"operation": "cancel"})
            self.scheduler.complete(export_id, "CANCELLED")
            future = self._export_waiters.get(export_id)
            if future is not None and not future.done():
                future.set_exception(ExportRequestError("CANCELLED", "export was cancelled"))
        else:
            self.scheduler.update_export(export_id, state="CANCEL_REQUESTED", error_code="CANCEL_REQUESTED")
            self.scheduler.record_export_event(export_id, "CANCEL_REQUESTED", {"operation": "cancel"})
            task = self._active_export_tasks.get(export_id)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                except asyncio.TimeoutError:
                    pass
        self._wake.set()
        return await self.export_status(session_id, export_id)

    async def export(
        self,
        *,
        task_id: str | None = None,
        conversation_url: str | None = None,
        session_id: str | None = None,
        scope: str = "recent",
        count: int = 10,
        on_busy: str = "reject",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper for the original inline export tool."""
        started = await self.export_start(
            task_id=task_id,
            conversation_url=conversation_url,
            session_id=session_id,
            scope=scope,
            count=count,
            on_busy=on_busy,
            request_id=request_id,
            legacy_inline=True,
        )
        export_id = str(started["export_id"])
        existing = self.exports.get_response(export_id)
        if existing is not None:
            return existing
        current = self.scheduler.get_export(export_id)
        if current and current["state"] in {"FAILED", "GONE", "RELEASED", "CANCELLED"}:
            raise ExportRequestError(str(current.get("error_code") or current["state"]), "export is no longer available")
        loop = asyncio.get_running_loop()
        future = self._export_waiters.get(export_id)
        if future is None:
            future = loop.create_future()
            self._export_waiters[export_id] = future
        existing = self.exports.get_response(export_id)
        if existing is not None:
            return existing
        try:
            response = await future
            if task_id:
                job = self.store.get(task_id)
                if job:
                    job.export_id = export_id
                    job.exported_at = utc_now()
                    job.export_scope = scope
                    job.export_message_count = int(response.get("message_count") or 0)
                    job.export_attachment_count = len(response.get("attachments") or [])
                    self.store.save(job)
            return response
        finally:
            self._export_waiters.pop(export_id, None)

    async def resume_session(
        self,
        session_id: str,
        prompt: str,
        *,
        mode: str | None = None,
        quality: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        allow_downgrade: bool | None = None,
        on_busy: str = "reject",
        request_id: str | None = None,
        upload_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
    ) -> Job:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if len(prompt) > 100_000:
            raise ValueError("prompt is too large; limit is 100000 characters")
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"session not found: {session_id}")
        if not session.conversation_url:
            raise ValueError("session has no ChatGPT conversation yet")
        last = self.store.get(session.last_task_id) if session.last_task_id else None
        if last and last.state in {"QUEUED", "STARTING", "UPLOADING", "ATTACHMENTS_VERIFIED", "SEND_PENDING", "RUNNING", "CANCEL_REQUESTED"}:
            raise ValueError(f"session is still processing task: {last.id}")
        selected_mode = mode or (last.mode if last else "consult")
        if selected_mode not in {"consult", "research", "critic"}:
            raise ValueError("mode must be consult, research, or critic")
        selected_quality = quality if quality is not None else (last.quality if last else "auto")
        selected_model = model if model is not None else (last.model if last else None)
        selected_effort = reasoning_effort if reasoning_effort is not None else (last.reasoning_effort if last else None)
        selected_downgrade = allow_downgrade if allow_downgrade is not None else (last.allow_downgrade if last else False)
        if selected_quality not in {"auto", "extra_high", "pro"}:
            raise ValueError("quality must be auto, extra_high, or pro")
        await self.start()
        upload_ids = list(upload_ids or [])
        file_refs = list(file_refs or [])
        if upload_ids and file_refs:
            raise ValueError("use upload_ids or file_refs, not both")
        job = Job.create(
            selected_mode,
            prompt,
            project=last.project if last else None,
            model=selected_model,
            quality=selected_quality,
            reasoning_effort=selected_effort,
            allow_downgrade=selected_downgrade,
            parent_id=last.id if last else None,
            session_id=session.id,
            file_refs=file_refs,
        )
        job.conversation_url = session.conversation_url
        retry_after = 180 if (job.quality == "pro" or (job.quality == "auto" and job.mode == "research")) else 30
        admission = self.scheduler.enqueue(
            job.id,
            job.mode,
            on_busy=on_busy,
            retry_after_seconds=retry_after,
            request_id=request_id,
            request_hash=self._task_request_hash(job),
        )
        if admission.get("idempotent"):
            existing = self.store.get(str(admission["task_id"]))
            if existing is None:
                raise SchedulerBusy(BusyInfo(str(admission["task_id"]), 0, 1, "REQUEST_IN_PROGRESS"))
            return existing
        try:
            attached = await self.uploads.attach(upload_ids, job.id)
            file_records = self.files.attach_files_to_task(session.id, job.id, file_refs) if file_refs else []
        except Exception:
            await self.uploads.release_job(job.id)
            self.scheduler.complete(job.id, "ERROR")
            raise
        job.upload_ids = upload_ids
        job.attachments = [item.metadata.public() for item in attached] + [record.public() for record in file_records]
        try:
            self.sessions.touch(session.id, conversation_url=session.conversation_url, last_task_id=job.id)
            self.store.save(job)
            self.scheduler.set_state(job.id, "QUEUED")
        except Exception:
            await self.uploads.release_job(job.id)
            self.files.release_task_files(job.id)
            self.scheduler.complete(job.id, "ERROR")
            raise
        self._wake.set()
        return job

    async def session_status(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"session not found: {session_id}")
        if session.state == "LIVE":
            session = self.sessions.touch(session.id)
        tab_open = bool(session.conversation_url and await self.browser.has_conversation_tab(session.conversation_url))
        return session.public(tab_open=tab_open)

    async def session_close(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"session not found: {session_id}")
        active_id = self.scheduler.status().get("active_task_id")
        if active_id:
            active_job = self.store.get(str(active_id))
            if active_job and active_job.session_id == session.id:
                raise SchedulerBusy(BusyInfo(str(active_id), 0, 30, "SESSION_BUSY"))
        for record in self.scheduler.list_exports(
            session_id=session.id,
            states=("QUEUED", "RUNNING", "CANCEL_REQUESTED", "READY"),
        ):
            export_id = str(record["export_id"])
            if record["state"] in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
                cancelled = await self.export_cancel(session.id, export_id)
                if cancelled["state"] not in {"CANCELLED", "FAILED", "GONE"}:
                    raise SchedulerBusy(BusyInfo(export_id, 0, 5, "EXPORT_CANCEL_PENDING"))
            elif record["state"] == "READY":
                await self.export_release(session.id, export_id)
        closed = self.sessions.close(session.id)
        self.exports.revoke_session(session.id)
        if closed.conversation_url:
            try:
                await self.browser.start()
                await self.browser.close_conversation_tabs(closed.conversation_url)
            except Exception:
                logger.exception("failed to close session tab %s", session.id)
        return closed.public(tab_open=False)

    async def session_list(self, *, include_dead: bool = True) -> list[dict[str, Any]]:
        sessions = self.sessions.all()
        if not include_dead:
            sessions = [session for session in sessions if session.state == "LIVE"]
        return [session.public() for session in sessions]

    async def _expire_sessions(self) -> None:
        active_id = self.scheduler.status().get("active_task_id")
        exclude: set[str] = set(self._active_session_ids)
        if active_id:
            active_job = self.store.get(str(active_id))
            if active_job and active_job.session_id:
                exclude.add(active_job.session_id)
        for session in self.sessions.expire_inactive(exclude=exclude):
            if session.conversation_url:
                try:
                    await self.browser.close_conversation_tabs(session.conversation_url)
                except Exception:
                    logger.exception("failed to close expired session tab %s", session.id)

    def get_export(self, export_id: str) -> dict[str, Any] | None:
        return self.exports.get(export_id)

    async def download_export_attachment(self, handle_id: str) -> tuple[bytes, str | None, str | None]:
        if "/" in handle_id:
            handle_id = handle_id.rstrip("/").rsplit("/", 1)[-1]
        handle = self.exports.consume(handle_id)
        try:
            result = await self.browser.download_attachment(
                handle.source_url,
                attachment_id=handle.attachment_id,
                conversation_url=handle.conversation_url,
            )
        except Exception as exc:
            self.scheduler.record_export_event(
                handle.export_id,
                "ATTACHMENT_DOWNLOAD_FAILED",
                {"error_type": type(exc).__name__, "operation": "attachment_download"},
            )
            raise
        if isinstance(result, bytes):
            body, content_type, disposition = result, None, None
        elif isinstance(result, tuple) and len(result) == 2:
            body, content_type = result
            disposition = None
        elif isinstance(result, tuple) and len(result) == 3:
            body, content_type, disposition = result
        else:
            raise TypeError("browser returned an invalid attachment response")
        if not isinstance(body, bytes):
            raise TypeError("browser attachment response body must be bytes")
        self.scheduler.record_export_event(
            handle.export_id,
            "ATTACHMENT_DOWNLOADED",
            {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "operation": "attachment_download",
            },
        )
        return body, content_type, disposition

    async def cancel(self, task_id: str) -> Job:
        job = self.get(task_id)
        if job.state not in TERMINAL_STATES:
            scheduler_state = self.scheduler.request_cancel(task_id)
            if scheduler_state == "CANCELLED":
                job.state = "CANCELLED"
                job.completed_at = utc_now()
                job.progress = "排队任务已取消。"
                self.scheduler.complete(task_id, "CANCELLED")
                await self.uploads.release_job(task_id)
                self.files.release_task_files(task_id)
            else:
                job.state = "CANCEL_REQUESTED"
                job.progress = "已请求取消；等待确认 ChatGPT 页面停止后释放占用。"
            self.store.save(job)
        self._wake.set()
        return job

    async def _worker_loop(self) -> None:
        while True:
            task_id = self.scheduler.reserve_next(
                os.getpid(),
                self._owner_token,
                allow_exports=self.export_owner,
            )
            if task_id:
                if task_id.startswith("exp_"):
                    request = self.scheduler.get_export(task_id)
                    if request is None:
                        self.scheduler.complete(task_id, "ERROR")
                    else:
                        session_id = request.get("session_id")
                        if session_id:
                            self._active_session_ids.add(session_id)
                        export_task = asyncio.create_task(
                            self._process_export(task_id, request),
                            name=f"chatgpt-oracle-export-{task_id}",
                        )
                        self._active_export_tasks[task_id] = export_task
                        try:
                            await asyncio.shield(export_task)
                        finally:
                            self._active_export_tasks.pop(task_id, None)
                            if session_id:
                                self._active_session_ids.discard(session_id)
                    continue
                job = self.store.get(task_id)
                if job:
                    if job.session_id:
                        self._active_session_ids.add(job.session_id)
                    try:
                        await self._process(job)
                    finally:
                        if job.session_id:
                            self._active_session_ids.discard(job.session_id)
                        await self.uploads.release_job(job.id)
                        finished = self.store.get(job.id)
                        if finished and (finished.state in TERMINAL_STATES or finished.state == "NEEDS_USER_INPUT"):
                            self.files.release_task_files(job.id)
                else:
                    self.scheduler.complete(task_id, "ERROR")
                continue
            if self.export_owner:
                await self._expire_sessions()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    async def _process_export(self, export_id: str, request: dict[str, Any]) -> None:
        future = self._export_waiters.get(export_id)
        message_spool = None
        try:
            self.scheduler.update_export(export_id, state="RUNNING")
            self.scheduler.set_state(export_id, "RUNNING", owner_pid=os.getpid())
            self.scheduler.record_export_event(export_id, "RUNNING")
            raw = await self.browser.export_conversation(
                request["conversation_url"],
                scope=request["scope"],
                count=request["count"],
                stream_pages=not bool(request.get("legacy_inline")),
            )
            if isinstance(raw, dict):
                candidate_spool = raw.get("message_spool")
                if callable(getattr(candidate_spool, "close", None)):
                    message_spool = candidate_spool
            scheduler_state = self.scheduler.status(export_id).get("scheduler_state")
            if scheduler_state == "CANCEL_REQUESTED":
                self.scheduler.update_export(export_id, state="CANCELLED")
                self.scheduler.record_export_event(export_id, "CANCELLED", {"operation": "cancel"})
                self.scheduler.complete(export_id, "CANCELLED")
                if future is not None and not future.done():
                    future.set_exception(ExportRequestError("CANCELLED", "export was cancelled"))
                return
            if isinstance(raw, dict) and raw.get("error_code"):
                raise ExportRequestError(str(raw["error_code"]), "browser could not read this conversation")
            if not isinstance(raw, dict) or not raw.get("messages"):
                raise ExportRequestError("NO_MESSAGES_READ", "browser did not expose any conversation messages")
            provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
            api_attempts = provenance.get("api_attempts") if isinstance(provenance.get("api_attempts"), list) else []
            self.scheduler.record_export_event(
                export_id,
                "SOURCE_ATTEMPTS",
                {"api_statuses": api_attempts, "source_summary": provenance},
            )
            source = str(raw.get("source") or "unknown")
            completeness = raw.get("completeness")
            reason = raw.get("reason") if isinstance(raw.get("reason"), dict) else {}
            payload = make_export(
                export_id=export_id,
                conversation_url=request["conversation_url"],
                scope=request["scope"],
                count=request["count"],
                browser_payload=raw,
                handles=self.exports,
                create_download_handles=bool(request.get("legacy_inline")),
            )
            del raw
            if not isinstance(completeness, dict):
                completeness = {
                    "messages": "unverified",
                    "attachments": "unverified",
                    "overall": "unverified",
                }
            states = {"verified", "unverified", "partial"}
            messages_state = completeness.get("messages")
            attachments_state = completeness.get("attachments")
            messages_state = messages_state if messages_state in states else "unverified"
            attachments_state = attachments_state if attachments_state in states else "unverified"
            manifest_attachments = payload.get("attachments") if isinstance(payload, dict) else None
            has_unavailable_attachment = isinstance(manifest_attachments, list) and any(
                not isinstance(item, dict) or item.get("downloadable") is not True
                for item in manifest_attachments
            )
            if has_unavailable_attachment:
                attachments_state = "partial"
            completeness = {
                "messages": messages_state,
                "attachments": attachments_state,
                "overall": "verified" if messages_state == attachments_state == "verified" else (
                    "partial" if "partial" in {messages_state, attachments_state} else "unverified"
                ),
            }
            if has_unavailable_attachment and not reason.get("attachments"):
                reason = {**reason, "attachments": "attachment_bytes_unavailable"}
            reason = ExportStore._safe_reasons(reason)
            snapshot_status = self.exports.put_snapshot(
                export_id,
                session_id=str(request["session_id"]),
                conversation_url=str(request["conversation_url"]),
                payload=payload,
                source=source,
                completeness=completeness,
                reason=reason,
            )
            self.scheduler.update_export(
                export_id,
                state="READY",
                source=source,
                completeness=completeness,
                reason=reason,
                message_count=snapshot_status["message_count"],
                attachment_count=snapshot_status["attachment_count"],
                views=snapshot_status["views"],
                expires_at=time.time() + self.settings.export_ttl_seconds,
            )
            self.scheduler.record_export_event(
                export_id,
                "READY",
                {
                    "source": source,
                    "message_count": snapshot_status["message_count"],
                    "attachment_count": snapshot_status["attachment_count"],
                    "messages_completeness": completeness.get("messages"),
                    "attachments_completeness": completeness.get("attachments"),
                    "overall_completeness": completeness.get("overall"),
                },
            )
            if request.get("legacy_inline"):
                encoded_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                response = dict(payload)
                response["inline"] = encoded_size <= self.settings.export_inline_max_bytes
                if not response["inline"]:
                    response["markdown"] = None
                    response["manifest"] = {
                        "conversation_url": request["conversation_url"],
                        "messages": [],
                        "attachments": response.get("attachments", []),
                    }
                    response["manifest_url"] = f"/exports/{export_id}"
                    response["download_endpoint"] = f"/exports/{export_id}"
                stored_payload = dict(payload)
                stored_payload["inline"] = response["inline"]
                if not response["inline"]:
                    stored_payload["manifest_url"] = response["manifest_url"]
                    stored_payload["download_endpoint"] = response["download_endpoint"]
                self.exports.put(export_id, stored_payload, response=response)
                if future is not None and not future.done():
                    future.set_result(response)
            else:
                response = {**snapshot_status, "accepted": True}
                if future is not None and not future.done():
                    future.set_result(response)
            current_session = self.sessions.get(str(request["session_id"]))
            if current_session and current_session.conversation_url == request["conversation_url"] and current_session.state == "LIVE":
                self.sessions.touch(str(request["session_id"]))
            self.scheduler.complete(export_id, "COMPLETE")
        except asyncio.CancelledError:
            cancellation_requested = self.scheduler.status(export_id).get("scheduler_state") == "CANCEL_REQUESTED"
            if self._stopping and not cancellation_requested:
                self.scheduler.update_export(export_id, state="QUEUED", error_code=None)
                self.scheduler.record_export_event(
                    export_id,
                    "REQUEUED_AFTER_STOP",
                    {"recovery_reason": "manager_stopped"},
                )
                self.scheduler.complete(export_id, "QUEUED")
                if future is not None and not future.done():
                    future.set_exception(RuntimeError("export manager stopped"))
            else:
                self.scheduler.update_export(export_id, state="CANCELLED", error_code="CANCELLED")
                self.scheduler.record_export_event(export_id, "CANCELLED", {"operation": "cancel"})
                self.scheduler.complete(export_id, "CANCELLED")
                if future is not None and not future.done():
                    future.set_exception(ExportRequestError("CANCELLED", "export was cancelled"))
        except Exception as exc:
            scheduler_state = self.scheduler.status(export_id).get("scheduler_state")
            if scheduler_state == "CANCEL_REQUESTED":
                self.scheduler.update_export(export_id, state="CANCELLED", error_code="CANCELLED")
                self.scheduler.record_export_event(export_id, "CANCELLED", {"operation": "cancel"})
                self.scheduler.complete(export_id, "CANCELLED")
                error = ExportRequestError("CANCELLED", "export was cancelled")
            else:
                if isinstance(exc, ExportCapacityError):
                    code = "EXPORT_CAPACITY"
                    state = "FAILED"
                    detail = type(exc).__name__
                elif isinstance(exc, BrowserNeedsUserAction):
                    code = "NEEDS_USER_INPUT"
                    state = "NEEDS_USER_INPUT"
                    detail = exc.instructions[:256]
                elif isinstance(exc, ExportRequestError):
                    code = exc.code
                    state = "FAILED"
                    detail = type(exc).__name__
                else:
                    code = "EXPORT_FAILED"
                    state = "FAILED"
                    detail = type(exc).__name__
                self.scheduler.update_export(export_id, state=state, error_code=code, error_detail=detail)
                self.scheduler.record_export_event(
                    export_id,
                    state,
                    {"error_code": code, "error_type": type(exc).__name__},
                )
                self.scheduler.complete(export_id, "ERROR")
                error = exc
            if future is not None and not future.done():
                future.set_exception(error)
        finally:
            if message_spool is not None:
                message_spool.close()

    async def _process(self, job: Job) -> None:
        receipt: SubmitReceipt | None = None
        try:
            owner_pid = os.getpid()
            recovering = job.state == "RUNNING" and bool(job.conversation_url)
            job.state = "STARTING" if not recovering else "RUNNING"
            job.started_at = job.started_at or utc_now()
            job.attempts += 1
            job.progress = "正在连接专用 ChatGPT 浏览器。"
            if job.session_id:
                self.sessions.touch(job.session_id, conversation_url=job.conversation_url, last_task_id=job.id)
            self.store.save(job)
            if recovering:
                receipt = SubmitReceipt(
                    conversation_url=job.conversation_url or "",
                    assistant_count_before=job.assistant_count_before,
                    selected_model=job.resolved_model,
                    requested_quality=job.quality,
                    requested_reasoning_effort=job.reasoning_effort,
                    resolved_model=job.resolved_model,
                    resolved_reasoning_effort=job.resolved_reasoning_effort,
                    selection_source=job.selection_source,
                    selection_verified_at=job.selection_verified_at,
                )
            elif job.parent_id and job.conversation_url:
                uploads = await self.uploads.take_for_job(job.id, job.upload_ids)
                uploads.extend(self.files.files_for_task(job.id))

                async def on_upload_verified(metadata: list[dict[str, Any]]) -> None:
                    if (self.store.get(job.id) or job).state == "CANCEL_REQUESTED":
                        raise UploadError("task was cancelled before the message was sent")
                    job.attachments = metadata
                    job.state = "ATTACHMENTS_VERIFIED"
                    job.progress = "附件已在 ChatGPT 页面验证，准备发送消息。"
                    self.store.save(job)
                    job.state = "SEND_PENDING"
                    job.progress = "附件已验证，正在发送消息。"
                    self.store.save(job)

                if uploads:
                    receipt = await self.browser.continue_conversation(
                        job.conversation_url,
                        job.prompt,
                        attachments=uploads,
                        on_upload_verified=on_upload_verified,
                    )
                else:
                    receipt = await self.browser.continue_conversation(job.conversation_url, job.prompt)
            else:
                prompt = job.prompt
                if job.mode == "critic":
                    prompt = (
                        "你是一个独立的批评者。请先指出下面问题中的隐含假设、风险和反例，"
                        "再给出可执行建议。\n\n" + prompt
                    )
                effective_quality = job.quality if job.quality != "auto" else QUALITY_DEFAULTS[job.mode]
                uploads = await self.uploads.take_for_job(job.id, job.upload_ids)
                uploads.extend(self.files.files_for_task(job.id))

                async def on_upload_verified(metadata: list[dict[str, Any]]) -> None:
                    if (self.store.get(job.id) or job).state == "CANCEL_REQUESTED":
                        raise UploadError("task was cancelled before the message was sent")
                    job.attachments = metadata
                    job.state = "ATTACHMENTS_VERIFIED"
                    job.progress = "附件已在 ChatGPT 页面验证，准备发送消息。"
                    self.store.save(job)
                    job.state = "SEND_PENDING"
                    job.progress = "附件已验证，正在发送消息。"
                    self.store.save(job)

                if uploads:
                    job.state = "UPLOADING"
                    job.progress = "正在把附件流式传输到 ChatGPT 页面。"
                    self.store.save(job)
                if uploads:
                    receipt = await self.browser.submit(
                        job.mode,
                        prompt,
                        model=job.model,
                        quality=effective_quality,
                        reasoning_effort=job.reasoning_effort,
                        allow_downgrade=job.allow_downgrade,
                        attachments=uploads,
                        on_upload_verified=on_upload_verified,
                        **self._dedicated_submit_options(job),
                    )
                else:
                    receipt = await self.browser.submit(
                        job.mode,
                        prompt,
                        model=job.model,
                        quality=effective_quality,
                        reasoning_effort=job.reasoning_effort,
                        allow_downgrade=job.allow_downgrade,
                        **self._dedicated_submit_options(job),
                    )
            self._receipts[job.id] = receipt
            job.conversation_url = receipt.conversation_url
            if job.session_id:
                self.sessions.touch(job.session_id, conversation_url=job.conversation_url, last_task_id=job.id)
            job.assistant_count_before = receipt.assistant_count_before
            job.resolved_model = receipt.resolved_model
            job.resolved_reasoning_effort = receipt.resolved_reasoning_effort
            job.selection_source = "default" if job.quality == "auto" else receipt.selection_source
            job.selection_verified_at = receipt.selection_verified_at
            if receipt.attachments:
                job.attachments = receipt.attachments
            job.error = None
            job.error_code = None
            cancellation_requested = (self.store.get(job.id) or job).state == "CANCEL_REQUESTED"
            job.state = "CANCEL_REQUESTED" if cancellation_requested else "RUNNING"
            job.progress = (
                "已提交取消请求；等待 ChatGPT 页面停止。"
                if cancellation_requested
                else "任务已提交，等待 ChatGPT 完成。"
            )
            self.store.save(job)
            self.scheduler.set_state(job.id, job.state, owner_pid=owner_pid)
        except BrowserNeedsUserAction as exc:
            job.state = "NEEDS_USER_INPUT"
            job.user_action = exc.instructions
            job.error = exc.reason
            job.completed_at = utc_now()
            self.store.save(job)
            self.scheduler.complete(job.id, "NEEDS_USER_INPUT")
            return
        except ModelUnavailableError as exc:
            job.state = "MODEL_UNAVAILABLE"
            job.error_code = "MODEL_UNAVAILABLE"
            job.error = str(exc)
            job.user_action = f"请求的能力不可用：{exc.requested}。当前可用选项：{', '.join(exc.available)}"
            job.completed_at = utc_now()
            self.store.save(job)
            self.scheduler.complete(job.id, "MODEL_UNAVAILABLE")
            return
        except UploadError as exc:
            logger.exception("task %s failed during upload", job.id)
            cancelled = (self.store.get(job.id) or job).state == "CANCEL_REQUESTED"
            job.state = "CANCELLED" if cancelled else "ERROR"
            job.error_code = None if cancelled else "UPLOAD_ERROR"
            job.error = None if cancelled else str(exc)
            job.progress = "附件上传已取消。" if cancelled else None
            job.completed_at = utc_now()
            self.store.save(job)
            self.scheduler.complete(job.id, job.state)
            return
        except (BrowserTaskError, Exception) as exc:
            logger.exception("task %s failed during submit", job.id)
            job.state = "ERROR"
            job.error = str(exc)
            job.error_code = "BROWSER_ERROR"
            job.completed_at = utc_now()
            self.store.save(job)
            self.scheduler.complete(job.id, "ERROR")
            return

        deadline = time.monotonic() + self.settings.timeout_seconds(job.mode)
        poll_seconds = self.settings.poll_seconds(job.mode)
        last_result_hash: str | None = None
        stable_result_polls = 0
        while time.monotonic() < deadline:
            current = self.store.get(job.id)
            if not current:
                return
            if current.state == "CANCELLED":
                self.scheduler.complete(job.id, "CANCELLED")
                return
            self.scheduler.heartbeat(job.id, self._owner_token)
            try:
                snapshot = await self.browser.inspect(receipt)
            except BrowserNeedsUserAction as exc:
                job.state = "NEEDS_USER_INPUT"
                job.user_action = exc.instructions
                job.error = exc.reason
                job.completed_at = utc_now()
                self.store.save(job)
                self.scheduler.complete(job.id, "NEEDS_USER_INPUT")
                return
            except Exception as exc:
                logger.exception("task %s failed during poll", job.id)
                job.state = "ERROR"
                job.error = str(exc)
                job.completed_at = utc_now()
                self.store.save(job)
                self.scheduler.complete(job.id, "ERROR")
                return
            if current.state == "CANCEL_REQUESTED":
                if snapshot.state != "RUNNING":
                    job.state = "CANCELLED"
                    job.completed_at = utc_now()
                    job.progress = "ChatGPT 页面已停止，任务已取消。"
                    self.store.save(job)
                    self.scheduler.complete(job.id, "CANCELLED")
                    return
                job.state = "CANCEL_REQUESTED"
                job.progress = "等待 ChatGPT 页面停止后释放浏览器占用。"
                self.store.save(job)
                await asyncio.sleep(poll_seconds)
                continue
            job.last_checked_at = utc_now()
            job.conversation_url = snapshot.conversation_url or job.conversation_url
            if job.session_id:
                self.sessions.touch(job.session_id, conversation_url=job.conversation_url, last_task_id=job.id)
            job.progress = snapshot.progress
            job.sources = snapshot.sources
            if snapshot.state == "COMPLETE" and snapshot.result_text:
                content = snapshot.result_text.strip()
                if job.mode == "research":
                    current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if current_hash == last_result_hash:
                        stable_result_polls += 1
                    else:
                        last_result_hash = current_hash
                        stable_result_polls = 1
                    if stable_result_polls < 2:
                        job.state = "RUNNING"
                        job.progress = "研究报告已出现，等待页面完成最后渲染。"
                        self.store.save(job)
                        await asyncio.sleep(poll_seconds)
                        continue
                if job.conversation_url:
                    content = f"<!-- conversation_url: {job.conversation_url} -->\n\n{content}"
                self.store.write_result(job, content)
                job.state = "COMPLETE"
                job.completed_at = utc_now()
                job.user_action = None
                job.error = None
                job.error_code = None
                self.store.save(job)
                self.scheduler.complete(job.id, "COMPLETE")
                return
            if snapshot.state == "NEEDS_USER_INPUT":
                job.state = "NEEDS_USER_INPUT"
                job.user_action = snapshot.user_action
                job.completed_at = utc_now()
                self.store.save(job)
                return
            job.state = "RUNNING"
            self.store.save(job)
            await asyncio.sleep(poll_seconds)
        job.state = "TIMEOUT"
        job.completed_at = utc_now()
        job.error = "任务达到配置的等待上限；可通过 conversation_url 手动检查并继续。"
        self.store.save(job)
        self.scheduler.complete(job.id, "TIMEOUT")

    def public(self, task_id: str) -> dict[str, Any]:
        job = self.get(task_id)
        return {**public_job(job), "scheduler": self.scheduler.status(task_id)}

    def _dedicated_submit_options(self, job: Job) -> dict[str, Any]:
        dedicated = bool(job.session_id or isinstance(self.browser, ChatGPTBrowser))
        if not dedicated:
            return {}
        try:
            parameters = inspect.signature(self.browser.submit).parameters.values()
            if not any(parameter.name == "dedicated_page" or parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
                return {}
        except (TypeError, ValueError):
            return {"dedicated_page": True}
        return {"dedicated_page": True}
