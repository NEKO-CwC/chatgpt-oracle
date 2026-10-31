from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, unquote
from typing import Any

from .browser import (
    BrowserNeedsUserAction,
    BrowserTaskError,
    BrowserCapacityError,
    BrowserPageNotReady,
    BrowserUnresponsiveError,
    ChatGPTBrowser,
    ModelUnavailableError,
    SubmitReceipt,
    StreamedAttachment,
)
from .config import Settings
from .providers import provider_policy, gemini_account_index
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
from .browser_resources import ResourceLockTimeout, UIAdmissionDeferred
from .submission_diagnostics import submission_diagnostics, submission_phase
from .oracle_protocol import (
    BOOTSTRAP_VERSION, PROTOCOL_VERSION, compose_message, confirmed_bootstrap,
    normalized_text_sha256, public_protocol,
)
from playwright.async_api import Error as PlaywrightError

logger = logging.getLogger("chatgpt_oracle.jobs")
QUALITY_DEFAULTS = {"consult": "extra_high", "critic": "extra_high", "research": "pro"}
MAX_RUNTIME_RECOVERY_ACTIONS = 2


class _RuntimeRecoveryCancelled(Exception):
    """Cancellation observed before a runtime action's durable reservation."""


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
        self.policy = provider_policy(settings.provider)
        self.export_owner = export_owner
        self.store = JobStore(settings.jobs_dir)
        self.scheduler = Scheduler(
            settings.jobs_dir,
            export_audit_retention_seconds=settings.export_audit_retention_seconds,
            shared_pool_path=settings.browser_pool_path,
            namespace=settings.provider,
        )
        if browser is None and settings.provider == "gemini":
            from .gemini_browser import GeminiBrowser
            browser = GeminiBrowser(settings)
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
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
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

    @asynccontextmanager
    async def _browser_scope(self, session_id: str | None = None, conversation_url: str | None = None):
        scope = getattr(self.browser, "operation_scope", None)
        if scope is None:
            yield
        else:
            async with scope(session_id=self._browser_session_key(session_id), conversation_url=conversation_url):
                yield

    def _browser_session_key(self, session_id: str | None) -> str | None:
        if session_id is None or not self.settings.shared_browser:
            return session_id
        return f"{self.settings.provider}:{session_id}"

    async def _close_browser_pages(self, *, job: Job | None = None, session_id: str | None = None) -> None:
        close = getattr(self.browser, "close_owned_pages", None)
        if close is not None:
            key = (job.session_id or job.id) if job else session_id
            session_key = job.session_id if job else session_id
            session = self.sessions.get(session_key) if session_key else None
            keep = bool(session and session.state == "LIVE" and session.conversation_url)
            keep = keep or bool(job and job.state == "NEEDS_USER_INPUT")
            # Failed owned pages are evidence, even without a committed URL.
            # Their Session TTL / explicit close still bounds residency.
            keep = keep or bool(job and job.state in {"ERROR", "TIMEOUT"}
                                and (job.session_id or job.submission_state == "unconfirmed"))
            try:
                await self._call_browser(close, keep_for_user=keep, retain_key=self._browser_session_key(key), timeout=self.settings.browser_timeout_seconds)
            except (BrowserUnresponsiveError, PlaywrightError):
                recover = getattr(self.browser, "discard_unresponsive_pages", None)
                if recover is None:
                    raise
                await self._call_browser(recover, timeout=self.settings.browser_timeout_seconds)

    async def _call_browser(self, method, *args, timeout: float | None = None, cancel_event: asyncio.Event | None = None, **kwargs):
        seconds = timeout if timeout is not None else 3 * self.settings.browser_timeout_seconds
        call = asyncio.create_task(method(*args, **kwargs), name=f"browser-{getattr(method, '__name__', 'operation')}")
        cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            done, _ = await asyncio.wait(
                {call, cancellation} if cancellation is not None else {call},
                timeout=seconds, return_when=asyncio.FIRST_COMPLETED,
            )
            if call in done:
                try:
                    return call.result()
                except ResourceLockTimeout:
                    raise
                except TimeoutError as exc:
                    mark = getattr(self.browser, "mark_unresponsive", None)
                    if mark is not None:
                        mark()
                    raise BrowserUnresponsiveError("Browser operation exceeded its internal deadline; this request will not be resent.") from exc
            if cancellation is not None and cancellation in done:
                await self._cancel_browser_call(call)
                return None
            mark = getattr(self.browser, "mark_unresponsive", None)
            if mark is not None:
                mark()
            await self._cancel_browser_call(call)
            raise BrowserUnresponsiveError(
                f"Browser {getattr(method, '__name__', 'operation')} did not respond within {seconds:g} seconds; "
                "delivery may be unknown, and this request will not be automatically sent again."
            )
        except asyncio.CancelledError:
            await self._cancel_browser_call(call)
            raise
        finally:
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)

    async def _cancel_browser_call(self, call: asyncio.Task) -> None:
        call.cancel()
        done, _ = await asyncio.wait({call}, timeout=0.5)
        if not done:
            abort = getattr(self.browser, "abort_driver", None)
            if abort is not None:
                async with asyncio.timeout(12):
                    await abort()
            call.cancel()
            done, _ = await asyncio.wait({call}, timeout=1)
        if not done:
            raise BrowserUnresponsiveError("Browser cancellation did not finish after driver recovery; the Session requires cleanup.")
        # Retrieve the cancelled call's outcome. It may contain an ordinary
        # driver failure after pipe termination; the caller reports the timeout.
        outcome = (await asyncio.gather(call, return_exceptions=True))[0]
        if isinstance(outcome, Exception):
            logger.info("cancelled browser call finished outcome_type=%s", type(outcome).__name__)

    @asynccontextmanager
    async def browser_operation(self, operation_id: str | None = None, owner_token: str | None = None, *, cleanup: bool = False, session_id: str | None = None, diagnostic: bool = False):
        """Join named Session FIFO, or immediately admit an isolated diagnostic."""
        await self.start()
        operation_id = operation_id or f"browser_{uuid.uuid4().hex}"
        owner_token = owner_token or uuid.uuid4().hex
        acquired = self.scheduler.enqueue_browser_operation(
            operation_id, os.getpid(), owner_token, cleanup=cleanup, session_id=session_id, diagnostic=diagnostic,
        )
        state = "CANCELLED"
        queue_deadline = time.monotonic() + self.settings.browser_timeout_seconds
        try:
            while not acquired:
                acquired = self.scheduler.reserve_browser_operation(operation_id, os.getpid(), owner_token)
                if acquired:
                    break
                if time.monotonic() >= queue_deadline:
                    raise SchedulerBusy(BusyInfo(operation_id, 0, 5, "BROWSER_QUEUE_TIMEOUT"))
                await asyncio.sleep(0.05)
            acquired = True
            session_id = self.scheduler.operation_session(operation_id)
            session = self.sessions.get(session_id) if session_id else None
            async with self._browser_scope(session_id, session.conversation_url if session else None):
                try:
                    yield
                    state = "COMPLETE"
                finally:
                    await self._close_browser_pages(session_id=session_id)
        finally:
            if acquired and session_id and cleanup:
                session = self.sessions.get(session_id)
                if session and session.state == "DEAD" and not session.tab_cleanup_pending:
                    self.scheduler.release_session(session_id, after_task_id=operation_id)
            self.scheduler.complete(operation_id, state)
            if acquired and session_id:
                session = self.sessions.get(session_id)
                if not session or session.state == "DEAD" and not session.tab_cleanup_pending:
                    self.scheduler.release_session(session_id)
            self._wake.set()

    async def browser_status(self):
        async with self.browser_operation(diagnostic=True):
            return await self._call_browser(self.browser.browser_status)

    async def browser_capabilities(self):
        async with self.browser_operation(diagnostic=True):
            return await self._call_browser(self.browser.capabilities)

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
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        active_tasks = list(self._active_tasks.values())
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
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

    async def _validate_input_refs(self, upload_ids: list[str], file_refs: list[str], session_id: str | None) -> None:
        if self.settings.provider != "gemini":
            return
        if len(upload_ids) + len(file_refs) > 10:
            raise FileTransferError("TOO_MANY_FILES", "Gemini accepts at most 10 inputs per turn")
        metadata = [await self.uploads.metadata(upload_id) for upload_id in upload_ids]
        metadata.extend(self.files.get_file(session_id, ref) for ref in file_refs)
        for item in metadata:
            self.policy.validate_file(item.filename, item.mime_type, item.size)

    def _replay_original_intent(self, request_id: str | None, intent: dict[str, Any]) -> tuple[Job | None, str]:
        digest = hashlib.sha256(json.dumps(intent, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if request_id:
            for existing in self.store.all():
                if existing.request_id != request_id or not existing.request_intent_sha256:
                    continue
                if existing.request_intent_sha256 != digest:
                    raise IdempotencyConflict("request_id was already used with different task inputs")
                # Preserve the global ledger and its original hash contract.
                # Inherited defaults may have changed since this request, but
                # matching caller intent returns the original frozen task.
                task_id = self.scheduler.idempotent_task(request_id, self._task_request_hash(existing))
                if task_id == existing.id:
                    return existing, digest
        return None, digest

    async def _admission_cancelled(self, job: Job) -> bool:
        if self.scheduler.status(job.id).get("scheduler_state") != "CANCELLED":
            return False
        job.state = "CANCELLED"
        job.completed_at = utc_now()
        job.progress = "任务在附件准备期间被取消；未发送。"
        self.store.save(job)
        await self.uploads.release_job(job.id)
        self.files.release_task_files(job.id)
        return True

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
        on_busy: str = "queue",
        request_id: str | None = None,
        upload_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
        session_id: str | None = None,
    ) -> Job:
        if mode not in {"consult", "research", "critic"}:
            raise ValueError("mode must be consult, research, or critic")
        if quality not in self.policy.qualities:
            raise ValueError(f"quality must be {', '.join(self.policy.qualities)}")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if len(prompt) > 100_000:
            raise ValueError("prompt is too large; limit is 100000 characters")
        replay, intent_hash = self._replay_original_intent(request_id, {
            "operation": "submit", "session_id": session_id.strip() if session_id else None,
            "mode": mode, "prompt": prompt, "project": project, "model": model,
            "quality": quality, "reasoning_effort": reasoning_effort, "allow_downgrade": allow_downgrade,
            "upload_ids": list(upload_ids or []), "file_refs": list(file_refs or []),
        })
        if replay:
            return replay
        await self.start()
        upload_ids = list(upload_ids or [])
        file_refs = list(file_refs or [])
        if upload_ids and file_refs:
            raise ValueError("use upload_ids or file_refs, not both")
        if session_id:
            session_id = self.sessions.validate_id(session_id)
        elif file_refs:
            raise ValueError("file_refs require a named session")
        if file_refs and session_id and self.sessions.get(session_id) is None:
            raise FileTransferError("SESSION_NOT_FOUND", "named session not found", 404)
        await self._validate_input_refs(upload_ids, file_refs, session_id)
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
        job.upload_ids = upload_ids
        job.request_id = request_id
        job.request_intent_sha256 = intent_hash
        if request_id:
            existing_id = self.scheduler.idempotent_task(request_id, self._task_request_hash(job))
            if existing_id:
                existing = self.store.get(existing_id)
                if existing is None:
                    raise SchedulerBusy(BusyInfo(existing_id, 0, 1, "REQUEST_IN_PROGRESS"))
                return existing
        if session_id:
            existing_session = self.sessions.get(session_id)
            if existing_session and existing_session.conversation_url:
                raise ValueError(f"session_id is already bound; use {self.policy.name}_resume for an existing session")
            if existing_session and existing_session.last_task_id:
                previous = self.store.get(existing_session.last_task_id)
                if previous and previous.state not in TERMINAL_STATES:
                    raise ValueError(f"session is still processing task: {previous.id}; use {self.policy.name}_resume to queue a turn")
        retry_after = 180 if (quality == "pro" or (quality == "auto" and mode == "research")) else 30
        admission = self.scheduler.enqueue(
            job.id,
            mode,
            on_busy=on_busy,
            retry_after_seconds=retry_after,
            request_id=request_id,
            request_hash=self._task_request_hash(job),
            session_id=job.session_id,
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
        if await self._admission_cancelled(job):
            return job
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
        on_busy: str = "queue",
        request_id: str | None = None,
        upload_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
    ) -> Job:
        prompt = prompt.strip()
        if not prompt or len(prompt) > 100_000:
            raise ValueError("prompt must be nonempty and at most 100000 characters")
        replay, intent_hash = self._replay_original_intent(request_id, {
            "operation": "continue", "task_id": task_id, "prompt": prompt, "quality": quality,
            "model": model, "reasoning_effort": reasoning_effort, "allow_downgrade": allow_downgrade,
            "upload_ids": list(upload_ids or []), "file_refs": list(file_refs or []),
        })
        if replay:
            return replay
        parent = self.store.get(task_id)
        if not parent:
            raise KeyError(f"task not found: {task_id}")
        if quality is not None and quality not in self.policy.qualities:
            raise ValueError(f"quality must be {', '.join(self.policy.qualities)}")
        if not parent.conversation_url:
            raise ValueError("task has no conversation URL to continue")
        if parent.state not in {"COMPLETE", "NEEDS_USER_INPUT"}:
            raise ValueError(f"task is not ready for continuation: {parent.state}")
        prompt = prompt.strip()
        if not prompt or len(prompt) > 100_000:
            raise ValueError("prompt must be nonempty and at most 100000 characters")
        await self.start()
        upload_ids = list(upload_ids or [])
        file_refs = list(file_refs or [])
        if upload_ids and file_refs:
            raise ValueError("use upload_ids or file_refs, not both")
        if file_refs and not parent.session_id:
            raise ValueError("file_refs require a named session")
        await self._validate_input_refs(upload_ids, file_refs, parent.session_id)
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
        job.upload_ids = upload_ids
        job.request_id = request_id
        job.request_intent_sha256 = intent_hash
        parent_session = self.sessions.get(parent.session_id) if parent.session_id else None
        job.oracle_recovery_pending = bool(parent_session and (parent_session.state == "DEAD" or parent_session.expired))
        retry_after = 180 if (job.quality == "pro" or (job.quality == "auto" and job.mode == "research")) else 30
        admission = self.scheduler.enqueue(
            job.id,
            job.mode,
            on_busy=on_busy,
            retry_after_seconds=retry_after,
            request_id=request_id,
            request_hash=self._task_request_hash(job),
            session_id=job.session_id,
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
        if await self._admission_cancelled(job):
            return job
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

    def _canonical_conversation_url(self, value: str) -> str:
        try:
            return self.policy.canonical_url(value)
        except ValueError as exc:
            raise ExportRequestError("INVALID_CONVERSATION_URL", str(exc)) from exc

    @staticmethod
    def _url_session_id(conversation_url: str) -> str:
        return "url-" + hashlib.sha256(conversation_url.encode("utf-8")).hexdigest()[:24]

    def _existing_url_session(self, conversation_url: str) -> str | None:
        resident = {item["session_key"] for item in self.scheduler.status()["session_slots"]}
        matches = [session for session in self.sessions.all() if session.conversation_url == conversation_url]
        matches.sort(key=lambda session: (session.id not in resident, session.state != "LIVE"))
        return matches[0].id if matches else None

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
            resolved_session_id = job.session_id or self._existing_url_session(normalized) or self._url_session_id(normalized)
            session = self.sessions.get(resolved_session_id) or self.sessions.create(resolved_session_id)
            if session.conversation_url and session.conversation_url != normalized:
                raise ExportRequestError("SESSION_URL_MISMATCH", "task session is bound to a different conversation")
            self.sessions.touch(resolved_session_id, conversation_url=normalized, last_task_id=job.id)
            return resolved_session_id, normalized

        if not conversation_url:
            raise ExportRequestError("SOURCE_REQUIRED", "session_id, task_id, or conversation_url is required")
        normalized = self._canonical_conversation_url(conversation_url)
        resolved_session_id = self._existing_url_session(normalized) or self._url_session_id(normalized)
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
        on_busy: str = "queue",
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
                scheduling_session_id=self._existing_url_session(normalized_url) or resolved_session_id,
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
        if not self.policy.source_attachments:
            raise ExportRequestError("UNSUPPORTED_CAPABILITY", "Gemini source attachment downloads are not supported")
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
        if not self.policy.source_attachments:
            raise ExportRequestError("UNSUPPORTED_CAPABILITY", "Gemini source attachment downloads are not supported")
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
            operation_id = transfer_id
            while True:
                try:
                    async with self.browser_operation(operation_id, owner_token, session_id=self._existing_url_session(conversation_url) or session_id):
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
                                operation_id=operation_id,
                            ),
                        )
                    break
                except BrowserCapacityError:
                    operation_id = f"browser_{uuid.uuid4().hex}"
                    await asyncio.sleep(0.25)
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
            self._file_download_tasks.pop(transfer_id, None)
            self._file_download_owner_tokens.pop(transfer_id, None)

    def _record_file_download_progress(
        self, transfer_id: str, session_id: str, owner_token: str, offset: int, *, operation_id: str | None = None
    ) -> None:
        self.files.update_download_progress(transfer_id, session_id, offset)
        self.scheduler.heartbeat(operation_id or transfer_id, owner_token)

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
        on_busy: str = "queue",
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
        on_busy: str = "queue",
        request_id: str | None = None,
        upload_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
    ) -> Job:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if len(prompt) > 100_000:
            raise ValueError("prompt is too large; limit is 100000 characters")
        replay, intent_hash = self._replay_original_intent(request_id, {
            "operation": "resume", "session_id": session_id.strip(), "prompt": prompt, "mode": mode,
            "quality": quality, "model": model, "reasoning_effort": reasoning_effort,
            "allow_downgrade": allow_downgrade, "upload_ids": list(upload_ids or []), "file_refs": list(file_refs or []),
        })
        if replay:
            return replay
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"session not found: {session_id}")
        last = self.store.get(session.last_task_id) if session.last_task_id else None
        if not session.conversation_url and not last:
            raise ValueError(f"session has no {self.policy.title} conversation or preceding task yet")
        selected_mode = mode or (last.mode if last else "consult")
        if selected_mode not in {"consult", "research", "critic"}:
            raise ValueError("mode must be consult, research, or critic")
        selected_quality = quality if quality is not None else (last.quality if last else "auto")
        selected_model = model if model is not None else (last.model if last else None)
        selected_effort = reasoning_effort if reasoning_effort is not None else (last.reasoning_effort if last else None)
        selected_downgrade = allow_downgrade if allow_downgrade is not None else (last.allow_downgrade if last else False)
        if selected_quality not in self.policy.qualities:
            raise ValueError(f"quality must be {', '.join(self.policy.qualities)}")
        await self.start()
        upload_ids = list(upload_ids or [])
        file_refs = list(file_refs or [])
        if upload_ids and file_refs:
            raise ValueError("use upload_ids or file_refs, not both")
        await self._validate_input_refs(upload_ids, file_refs, session.id)
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
        job.upload_ids = upload_ids
        job.request_id = request_id
        job.request_intent_sha256 = intent_hash
        job.oracle_recovery_pending = session.state == "DEAD" or session.expired
        retry_after = 180 if (job.quality == "pro" or (job.quality == "auto" and job.mode == "research")) else 30
        admission = self.scheduler.enqueue(
            job.id,
            job.mode,
            on_busy=on_busy,
            retry_after_seconds=retry_after,
            request_id=request_id,
            request_hash=self._task_request_hash(job),
            session_id=job.session_id,
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
        if await self._admission_cancelled(job):
            return job
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
        tab_open = bool(
            session.state == "LIVE" and session.conversation_url
            and await self._call_browser(
                self.browser.has_conversation_tab, session.conversation_url,
                timeout=min(5.0, self.settings.browser_timeout_seconds),
            )
        )
        return self.public_session(session, tab_open=tab_open)

    async def session_close(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"session not found: {session_id}")
        active_id = self.scheduler.session_active(session_id)
        active_job = self.store.get(str(active_id)) if active_id else None
        if active_job and active_job.session_id == session_id:
            raise SchedulerBusy(BusyInfo(str(active_id), 0, 30, "SESSION_BUSY"))
        for task_id in self.scheduler.cancel_waiting_session_tasks(session_id):
            job = self.store.get(task_id)
            if job is not None:
                await self._admission_cancelled(job)
        # Cancel the session's running export before joining its occupied slot.
        # Waiting in FIFO first would put close behind the work it must stop.
        for record in self.scheduler.list_exports(session_id=session_id, states=("QUEUED", "RUNNING", "CANCEL_REQUESTED")):
            cancelled = await self.export_cancel(session_id, str(record["export_id"]))
            if cancelled["state"] not in {"CANCELLED", "FAILED", "GONE"}:
                raise SchedulerBusy(BusyInfo(str(record["export_id"]), 0, 5, "EXPORT_CANCEL_PENDING"))
        slots = self.scheduler.status()["session_slots"]
        resident = any(row["session_key"] == session_id for row in slots)
        owns_page = resident or session.tab_cleanup_pending
        if not owns_page:
            ownership = getattr(self.browser, "has_session_page", None)
            if ownership is not None:
                owns_page = await self._call_browser(
                    ownership, self._browser_session_key(session_id), timeout=min(5.0, self.settings.browser_timeout_seconds),
                )
            elif session.conversation_url:
                # Legacy adapters have no ownership index: only a known foreign
                # resident of this exact URL establishes a metadata-only alias.
                aliases_resident = any(
                    row["persistent"] and row["session_key"] != session_id
                    and (owner := self.sessions.get(row["session_key"])) is not None
                    and owner.conversation_url == session.conversation_url
                    for row in slots
                )
                owns_page = not aliases_resident
        if not owns_page:
            # A fifth Session that never obtained a lane has no browser page.
            # Closing it must not wait for all four generating Sessions to drain.
            closed = self.sessions.close(session_id)
            closed.tab_cleanup_pending = False
            self.sessions.save(closed)
            self.exports.revoke_session(session_id)
            return self.public_session(closed, tab_open=False)
        async with self.browser_operation(cleanup=True, session_id=session_id):
            return await self._session_close(session_id)

    async def _close_session_pages(self, session: ConversationSession) -> None:
        try:
            if session.conversation_url:
                start = getattr(self.browser, "start", None)
                if start is not None:
                    await self._call_browser(start)
                await self._call_browser(self.browser.close_conversation_tabs, session.conversation_url)
            close_retained = getattr(self.browser, "close_retained_pages", None)
            if close_retained is not None:
                await self._call_browser(close_retained, self._browser_session_key(session.id))
        except (BrowserUnresponsiveError, PlaywrightError):
            recover = getattr(self.browser, "discard_unresponsive_pages", None)
            if recover is None:
                raise
            await self._call_browser(recover, timeout=self.settings.browser_timeout_seconds)
        session.tab_cleanup_pending = False
        self.sessions.save(session)

    async def _session_close(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"session not found: {session_id}")
        active_id = self.scheduler.session_active(session_id)
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
        await self._close_session_pages(closed)
        return self.public_session(closed, tab_open=False)

    async def session_list(self, *, include_dead: bool = True) -> list[dict[str, Any]]:
        sessions = self.sessions.all()
        if not include_dead:
            sessions = [session for session in sessions if session.state == "LIVE"]
        return [self.public_session(session) for session in sessions]

    async def _expire_sessions(self) -> None:
        candidates = self.sessions.all()
        if not any(session.tab_cleanup_pending or session.state == "LIVE" and session.expired for session in candidates):
            return
        pending = self.scheduler.pending_session_ids() | self._active_session_ids
        self.sessions.expire_inactive(exclude=pending)
        for session in self.sessions.all():
            if session.state == "DEAD" and session.tab_cleanup_pending and session.id not in pending:
                async with self.browser_operation(cleanup=True, session_id=session.id):
                    # Admission may have refreshed the Session while expiry
                    # waited for its lane. Never apply a stale DEAD snapshot.
                    current = self.sessions.get(session.id)
                    if current and current.state == "DEAD" and current.tab_cleanup_pending:
                        await self._close_session_pages(current)

    def get_export(self, export_id: str) -> dict[str, Any] | None:
        return self.exports.get(export_id)

    async def download_export_attachment(self, handle_id: str) -> tuple[bytes, str | None, str | None]:
        return await self._download_export_attachment(handle_id)

    async def _download_export_attachment(self, handle_id: str) -> tuple[bytes, str | None, str | None]:
        if "/" in handle_id:
            handle_id = handle_id.rstrip("/").rsplit("/", 1)[-1]
        handle = self.exports.consume(handle_id)
        try:
            while True:
                try:
                    async with self.browser_operation(session_id=self._existing_url_session(handle.conversation_url) or handle.session_id):
                        result = await self.browser.download_attachment(
                            handle.source_url,
                            attachment_id=handle.attachment_id,
                            conversation_url=handle.conversation_url,
                        )
                    break
                except BrowserCapacityError:
                    await asyncio.sleep(0.25)
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
        if job.state == "NEEDS_USER_INPUT":
            async with self.browser_operation(cleanup=True, session_id=job.session_id):
                close_retained = getattr(self.browser, "close_retained_pages", None)
                if close_retained is not None and not job.session_id:
                    await close_retained(self._browser_session_key(job.session_id or job.id))
                if job.conversation_url and not job.session_id:
                    await self.browser.close_conversation_tabs(job.conversation_url)
                job.state = "CANCELLED"
                job.completed_at = utc_now()
                self.store.save(job)
                self.scheduler.complete(job.id, "CANCELLED")
            return job
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
                job.cancel_requested_at = job.cancel_requested_at or utc_now()
                job.progress = "已请求取消；等待确认 ChatGPT 页面停止后释放占用。"
            self.store.save(job)
            self._cancel_events.setdefault(task_id, asyncio.Event()).set()
        self._wake.set()
        return job

    async def _worker_loop(self) -> None:
        while True:
            task_id = self.scheduler.reserve_next(
                os.getpid(), self._owner_token, allow_exports=self.export_owner,
                exclude=set(self._active_tasks),
            )
            if task_id:
                task = asyncio.create_task(self._run_reserved(task_id), name=f"chatgpt-oracle-{task_id}")
                self._active_tasks[task_id] = task
                if task_id.startswith("exp_"):
                    self._active_export_tasks[task_id] = task
                # Dispatch other Session lanes while this one generates.
                continue
            if self.export_owner:
                await self._expire_sessions()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

    async def _run_reserved(self, task_id: str) -> None:
        session_id = self.scheduler.operation_session(task_id)
        session = self.sessions.get(session_id) if session_id else None
        if session_id:
            self._active_session_ids.add(session_id)
        try:
            async with self._browser_scope(session_id, session.conversation_url if session else None):
                if task_id.startswith("exp_"):
                    request = self.scheduler.get_export(task_id)
                    if request is None:
                        self.scheduler.complete(task_id, "ERROR")
                    else:
                        await self._process_export(task_id, request)
                    return
                job = self.store.get(task_id)
                if job is None:
                    self.scheduler.complete(task_id, "ERROR")
                    return
                if job.state in TERMINAL_STATES or job.state == "NEEDS_USER_INPUT":
                    self.scheduler.complete(task_id, job.state)
                    return
                await self._process(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Session operation %s failed outside task processing", task_id)
            job = self.store.get(task_id)
            if job is not None:
                job.state = "ERROR"
                job.error_code = "BROWSER_CLEANUP_FAILED"
                job.error = str(exc)
                job.user_action = "页面资源清理未确认；请关闭该 Session 后再继续，勿重复提交原问题。"
                job.completed_at = utc_now()
                self.store.save(job)
            if task_id.startswith("exp_"):
                self.scheduler.update_export(task_id, state="FAILED", error_code="BROWSER_CLEANUP_FAILED")
            # The Session remains blocked until explicit cleanup. Other Sessions
            # can proceed, and the caller sees an accurate failure.
            self.scheduler.complete(task_id, "RECOVERY_REQUIRED")
        finally:
            if not task_id.startswith("exp_"):
                finished = self.store.get(task_id)
                if finished and (finished.state in TERMINAL_STATES or finished.state == "NEEDS_USER_INPUT"):
                    await self.uploads.release_job(task_id)
                    self.files.release_task_files(task_id)
            if session_id:
                self._active_session_ids.discard(session_id)
            self._active_tasks.pop(task_id, None)
            self._active_export_tasks.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
            self._wake.set()

    async def _process_export(self, export_id: str, request: dict[str, Any]) -> None:
        try:
            await self._process_export_body(export_id, request)
        finally:
            await self._close_browser_pages(session_id=self.scheduler.operation_session(export_id) or str(request["session_id"]))
            record = self.scheduler.get_export(export_id) or {}
            state = record.get("state")
            terminal = {"READY": "COMPLETE", "QUEUED": "QUEUED", "CANCELLED": "CANCELLED", "NEEDS_USER_INPUT": "NEEDS_USER_INPUT"}.get(state, "ERROR")
            if terminal == "QUEUED":
                self.scheduler.defer_capacity(export_id)
            else:
                self.scheduler.complete(export_id, terminal)

    async def _process_export_body(self, export_id: str, request: dict[str, Any]) -> None:
        future = self._export_waiters.get(export_id)
        message_spool = None
        try:
            self.scheduler.update_export(export_id, state="RUNNING")
            self.scheduler.set_state(export_id, "RUNNING", owner_pid=os.getpid())
            self.scheduler.record_export_event(export_id, "RUNNING")
            raw = await self._call_browser(self.browser.export_conversation,
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
                origin_receipts=self._origin_receipts(request["conversation_url"]),
                allow_unique_text_receipts=bool(
                    request["scope"] == "all" and source in {"api", "api_paged"}
                    and isinstance(completeness, dict) and completeness.get("messages") == "verified"
                ),
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
        except asyncio.CancelledError:
            cancellation_requested = self.scheduler.status(export_id).get("scheduler_state") == "CANCEL_REQUESTED"
            if self._stopping and not cancellation_requested:
                self.scheduler.update_export(export_id, state="QUEUED", error_code=None)
                self.scheduler.record_export_event(
                    export_id,
                    "REQUEUED_AFTER_STOP",
                    {"recovery_reason": "manager_stopped"},
                )
                if future is not None and not future.done():
                    future.set_exception(RuntimeError("export manager stopped"))
            else:
                self.scheduler.update_export(export_id, state="CANCELLED", error_code="CANCELLED")
                self.scheduler.record_export_event(export_id, "CANCELLED", {"operation": "cancel"})
                if future is not None and not future.done():
                    future.set_exception(ExportRequestError("CANCELLED", "export was cancelled"))
        except BrowserCapacityError:
            self.scheduler.update_export(export_id, state="QUEUED", error_code="BROWSER_TAB_LIMIT")
        except Exception as exc:
            scheduler_state = self.scheduler.status(export_id).get("scheduler_state")
            if scheduler_state == "CANCEL_REQUESTED":
                self.scheduler.update_export(export_id, state="CANCELLED", error_code="CANCELLED")
                self.scheduler.record_export_event(export_id, "CANCELLED", {"operation": "cancel"})
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
                error = exc
            if future is not None and not future.done():
                future.set_exception(error)
        finally:
            if message_spool is not None:
                message_spool.close()

    async def _process(self, job: Job) -> None:
        cancelled = False
        try:
            with submission_diagnostics(self.settings.jobs_dir, job.id, self.settings.provider):
                try:
                    await self._process_body(job)
                finally:
                    await self._persist_page_probe(job)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            current = self.store.get(job.id) or job
            if current.error_code != "BROWSER_UI_BUSY":
                await self._close_browser_pages(job=current)
            if current.error_code == "BROWSER_TAB_LIMIT" or (
                current.error_code == "BROWSER_UI_BUSY" and current.state == "QUEUED"
            ):
                self.scheduler.defer_capacity(job.id)
            elif cancelled and current.state in {"RUNNING", "CANCEL_REQUESTED"} and current.conversation_url:
                self.scheduler.requeue_confirmed_poll(job.id)
            elif cancelled:
                current.state = "ERROR"
                current.error_code = "INTERRUPTED_BEFORE_RECEIPT"
                current.error = "Worker stopped before the send could be confirmed; do not automatically resubmit."
                self.store.save(current)
                self.scheduler.complete(job.id, "ERROR")
            else:
                self.scheduler.complete(job.id, current.state)
                if current.session_id:
                    session = self.sessions.get(current.session_id)
                    if session and session.state == "DEAD" and not session.tab_cleanup_pending:
                        self.scheduler.release_session(current.session_id, after_task_id=job.id)

    async def _persist_page_probe(self, job: Job) -> None:
        """Keep page-side console/network failures after the tab is released.

        The probe lives in the page and in the process ring; without this
        copy a failed turn's evidence disappears with its tab.
        """
        drain = getattr(self.browser, "drain_page_probe", None)
        snapshot = getattr(self.browser, "page_probe_snapshot", None)
        page = getattr(self.browser, "_scope", None)
        if page is None:
            page = getattr(self.browser, "_legacy_operation", None)
        try:
            pages = list((page.owned_pages if page is not None else {}).keys())
            target = None
            for candidate in pages:
                if not candidate.is_closed():
                    target = candidate
            if target is not None and callable(drain):
                await drain(target, reason="job_finished")
            events = []
            for candidate in pages:
                if callable(snapshot):
                    events.extend(snapshot(candidate))
            if not events:
                return
            current = self.store.get(job.id) or job
            setattr(current, "page_probe_events", events[-60:])
            self.store.save(current)
        except Exception:
            logger.warning("Page probe could not be persisted for %s", job.id, exc_info=True)

    async def _process_body(self, job: Job) -> None:
        receipt: SubmitReceipt | None = None
        try:
            owner_pid = os.getpid()
            recovering = job.state in {"RUNNING", "CANCEL_REQUESTED"} and bool(job.conversation_url)
            if job.session_id and job.parent_id and not job.conversation_url:
                session = self.sessions.get(job.session_id)
                job.conversation_url = session.conversation_url if session else None
                if not job.conversation_url:
                    raise BrowserTaskError("The preceding Session turn has no confirmed conversation URL; continuation was not sent.")
            job.state = "STARTING" if not recovering else job.state
            job.started_at = job.started_at or utc_now()
            job.attempts += 1
            job.progress = "正在连接专用 ChatGPT 浏览器。"
            if job.session_id:
                self.sessions.touch(job.session_id, conversation_url=job.conversation_url)
            self.store.save(job)
            effective_quality = job.quality if job.quality != "auto" else self.policy.defaults[job.mode]
            if not recovering:
                await self._freeze_sending_text(job)
                current = self.store.get(job.id) or job
                if current.state in {"CANCEL_REQUESTED", "CANCELLED"}:
                    job.state = "CANCELLED"
                    job.cancel_requested_at = current.cancel_requested_at
                    job.completed_at = utc_now()
                    job.progress = "发送准备期间取消；未发送本轮消息。"
                    self.store.save(job)
                    return
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
                    research_expected=job.research_expected,
                    research_start_state=job.research_start_state,
                    research_user_message_id=job.research_user_message_id,
                    research_user_prompt_sha256=job.research_user_prompt_sha256,
                    research_assistant_message_id=job.research_assistant_message_id,
                    research_observed=job.research_observed,
                    research_state=job.research_state,
                    research_run_id=job.research_run_id,
                    research_report_id=job.research_report_id,
                    user_message_ids_before=list(job.user_message_ids_before),
                    assistant_message_ids_before=list(job.assistant_message_ids_before),
                    submitted_prompt_sha256=job.submitted_prompt_sha256,
                    current_user_message_id=job.current_user_message_id,
                    current_assistant_message_id=job.current_assistant_message_id,
                    submission_state=job.submission_state,
                    submission_verified_at=job.submission_verified_at,
                    runtime_recovery_attempts=job.runtime_recovery_attempts,
                    account_index=job.account_index,
                    account_attempts=list(job.account_attempts),
                    account_switch=job.account_switch,
                )
            elif job.conversation_url:
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
                    receipt = await self._call_browser(self.browser.continue_conversation,
                        job.conversation_url,
                        job.sending_text,
                        model=job.model,
                        quality=effective_quality,
                        reasoning_effort=job.reasoning_effort,
                        allow_downgrade=job.allow_downgrade,
                        attachments=uploads,
                        on_upload_verified=on_upload_verified,
                        **self._continuation_options(job),
                        **self._prepared_submission_options(job, self.browser.continue_conversation),
                    )
                else:
                    receipt = await self._call_browser(self.browser.continue_conversation,
                        job.conversation_url,
                        job.sending_text,
                        model=job.model,
                        quality=effective_quality,
                        reasoning_effort=job.reasoning_effort,
                        allow_downgrade=job.allow_downgrade,
                        **self._continuation_options(job),
                        **self._prepared_submission_options(job, self.browser.continue_conversation),
                    )
            else:
                prompt = job.sending_text
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
                    job.state = "STARTING"
                    job.progress = "正在准备 ChatGPT 页面并验证模型档位；附件尚未在网页验证。"
                    self.store.save(job)
                if uploads:
                    receipt = await self._call_browser(self.browser.submit,
                        job.mode,
                        prompt,
                        model=job.model,
                        quality=effective_quality,
                        reasoning_effort=job.reasoning_effort,
                        allow_downgrade=job.allow_downgrade,
                        attachments=uploads,
                        on_upload_verified=on_upload_verified,
                        **self._dedicated_submit_options(job),
                        **self._prepared_submission_options(job, self.browser.submit),
                    )
                else:
                    receipt = await self._call_browser(self.browser.submit,
                        job.mode,
                        prompt,
                        model=job.model,
                        quality=effective_quality,
                        reasoning_effort=job.reasoning_effort,
                        allow_downgrade=job.allow_downgrade,
                        **self._dedicated_submit_options(job),
                        **self._prepared_submission_options(job, self.browser.submit),
                    )
            self._remember_submission_receipt(job, receipt)
            job.error = None
            job.error_code = None
            cancellation_requested = (self.store.get(job.id) or job).state == "CANCEL_REQUESTED"
            pending_submission = (receipt.submission_state == "unconfirmed"
                                  and callable(getattr(self.browser, "inspect_submission", None)))
            if not cancellation_requested and (
                (receipt.submission_state == "unconfirmed" and not pending_submission)
                or (recovering and self._requires_turn_receipts() and receipt.submission_state == "legacy_unverified")
            ):
                job.state = "ERROR"
                job.error_code = "SEND_UNCONFIRMED" if not recovering else "TURN_IDENTITY_UNAVAILABLE"
                job.error = "This turn's send/identity was not confirmed within its bounded observation; delivery may have occurred. Inspect the original URL; do not resend the prompt."
                job.progress = "本轮发送身份未确认；保留原任务与 URL，仅作只读核查，不重提问题。"
                job.completed_at = utc_now()
                self.store.save(job)
                return
            job.state = "CANCEL_REQUESTED" if cancellation_requested else "SEND_PENDING" if pending_submission else "RUNNING"
            job.progress = (
                "已提交取消请求；等待 ChatGPT 页面停止。"
                if cancellation_requested
                else "发送回执已保存，等待原页面建立会话；不会再次提交。" if pending_submission
                else "本轮发送已确认，等待 ChatGPT 完成。" if receipt.submission_state == "confirmed"
                else "正在观察原会话，尚无本轮发送身份确认。"
            )
            self.store.save(job)
            self.scheduler.set_state(job.id, "RUNNING" if pending_submission else job.state, owner_pid=owner_pid)
        except BrowserCapacityError as exc:
            job.state = "RUNNING" if recovering else "QUEUED"
            job.error_code = "BROWSER_TAB_LIMIT"
            job.progress = str(exc)
            self.store.save(job)
            return
        except UIAdmissionDeferred as exc:
            # This exception is emitted only by the initial admission gate,
            # never by an upload, a send or post-send recovery.
            current = self.store.get(job.id) or job
            job.attempts = max(0, job.attempts - 1)
            elapsed = time.time() - datetime.fromisoformat(job.started_at).timestamp()
            if current.state in {"CANCEL_REQUESTED", "CANCELLED"}:
                job.state = "CANCELLED"
                job.cancel_requested_at = current.cancel_requested_at
                job.completed_at = utc_now()
                job.error_code = None
                job.progress = "等待共享浏览器交互期间取消；未发送消息。"
            elif elapsed >= self.settings.timeout_seconds(job.mode):
                job.state = "ERROR"
                job.error_code = "BROWSER_UI_WAIT_TIMEOUT"
                job.error = "Initial browser UI admission exceeded the task wait budget; no message was sent."
                job.completed_at = utc_now()
            else:
                job.state = "QUEUED"
                job.error_code = "BROWSER_UI_BUSY"
                job.error = None
                job.progress = "等待共享浏览器交互；保留同一任务，尚未发送。"
            self.store.save(job)
            return
        except (BrowserUnresponsiveError, ResourceLockTimeout) as exc:
            job.state = "ERROR"
            job.error_code = "BROWSER_LOCK_TIMEOUT" if isinstance(exc, ResourceLockTimeout) else "BROWSER_UNRESPONSIVE"
            job.error = str(exc)
            job.progress = None
            job.completed_at = utc_now()
            self.store.save(job)
            return
        except BrowserPageNotReady as exc:
            job.state = "ERROR"
            job.error_code = getattr(exc, "code", "BROWSER_NOT_READY")
            job.error = str(exc)
            job.progress = None
            job.completed_at = utc_now()
            self.store.save(job)
            return
        except BrowserNeedsUserAction as exc:
            job.state = "NEEDS_USER_INPUT"
            job.user_action = exc.instructions
            job.error = exc.reason
            job.user_input_kind = getattr(exc, "user_input_kind", "manual_browser")
            job.error_code = getattr(exc, "code", None)
            job.completed_at = utc_now()
            self.store.save(job)
            return
        except ModelUnavailableError as exc:
            self._persist_account_metadata(job, exc)
            job.state = "MODEL_UNAVAILABLE"
            job.error_code = "MODEL_UNAVAILABLE"
            job.progress = None
            job.error = str(exc)
            job.user_action = f"请求的能力不可用：{exc.requested}。当前可用选项：{', '.join(exc.available)}"
            job.completed_at = utc_now()
            self.store.save(job)
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
            return
        except (BrowserTaskError, Exception) as exc:
            logger.exception("task %s failed during submit", job.id)
            self._persist_account_metadata(job, exc)
            job.state = "ERROR"
            job.error = str(exc)
            job.error_code = ("SEND_UNCONFIRMED" if job.id in self._receipts
                              and job.submission_state == "unconfirmed"
                              else getattr(exc, "code", "BROWSER_ERROR"))
            job.progress = None
            job.completed_at = utc_now()
            self.store.save(job)
            return

        observation_started = time.monotonic()
        wait_mode = "research" if job.research_observed else job.mode
        deadline = observation_started + self.settings.timeout_seconds(wait_mode)
        poll_seconds = self.settings.poll_seconds(wait_mode)
        last_result_hash: str | None = None
        stable_result_polls = 0
        last_snapshot = None
        # A bounded observation proves the turn's identity, not the stream's
        # health. Re-observing the same page every poll is not evidence that
        # the page is still live, so idle evidence is counted and logged; a
        # page reload is a browser-side action and is therefore never taken
        # automatically (it could steal input focus or restart the stream).
        settled_polls = 0
        while time.monotonic() < deadline:
            current = self.store.get(job.id)
            if not current:
                return
            if current.state == "CANCELLED":
                return
            if current.state == "CANCEL_REQUESTED":
                await self._finish_job_cancellation(job, receipt)
                return
            self.scheduler.heartbeat(job.id, self._owner_token)
            try:
                observe = (getattr(self.browser, "inspect_submission", None)
                           if receipt.submission_state == "unconfirmed" else None)
                snapshot = await self._call_browser(
                    observe or self.browser.inspect, receipt, timeout=self.settings.browser_timeout_seconds,
                    cancel_event=self._cancel_events.setdefault(job.id, asyncio.Event()),
                )
            except BrowserCapacityError as exc:
                job.state = "RUNNING"
                job.error_code = "BROWSER_TAB_LIMIT"
                job.progress = str(exc)
                self.store.save(job)
                return
            except BrowserNeedsUserAction as exc:
                job.state = "NEEDS_USER_INPUT"
                job.user_action = exc.instructions
                job.error = exc.reason
                job.user_input_kind = getattr(exc, "user_input_kind", "manual_browser")
                job.error_code = getattr(exc, "code", None)
                job.completed_at = utc_now()
                self.store.save(job)
                return
            except (BrowserUnresponsiveError, ResourceLockTimeout) as exc:
                job.state = "ERROR"
                job.error_code = "BROWSER_LOCK_TIMEOUT" if isinstance(exc, ResourceLockTimeout) else "BROWSER_UNRESPONSIVE"
                job.error = str(exc)
                job.progress = None
                job.completed_at = utc_now()
                self.store.save(job)
                return
            except Exception as exc:
                logger.exception("task %s failed during poll", job.id)
                self._persist_turn_receipt(job, receipt)
                self._persist_account_metadata(job, exc)
                job.state = "ERROR"
                job.error = str(exc)
                job.error_code = getattr(exc, "code", "BROWSER_ERROR")
                job.completed_at = utc_now()
                self.store.save(job)
                return
            # A cancellation may arrive while the bounded inspection is in
            # flight. Read the durable intent again before publishing RUNNING.
            current = self.store.get(job.id)
            if not current or current.state == "CANCELLED":
                return
            if current.state == "CANCEL_REQUESTED":
                await self._finish_job_cancellation(job, receipt, snapshot=snapshot)
                return
            job.last_checked_at = utc_now()
            last_snapshot = snapshot
            self._persist_turn_receipt(job, receipt)
            job.conversation_url = snapshot.conversation_url or job.conversation_url
            if job.session_id:
                self.sessions.touch(job.session_id, conversation_url=job.conversation_url)
            job.progress = snapshot.progress
            job.sources = snapshot.sources
            self._remember_research_observation(job, receipt, snapshot)
            if wait_mode != "research" and job.research_observed:
                # Select the actual workflow budget once, from the same
                # observation origin. Repeated progress never moves its end.
                wait_mode = "research"
                deadline = observation_started + self.settings.timeout_seconds(wait_mode)
                poll_seconds = self.settings.poll_seconds(wait_mode)
                submission_phase("research_wait_budget_selected",
                                 budget_seconds=self.settings.timeout_seconds(wait_mode),
                                 requested_mode=job.mode)
            if snapshot.state == "RECOVERABLE_ERROR":
                if not await self._recover_runtime_error(job, receipt, snapshot):
                    return
                # A reload/replacement invalidates this snapshot. Observe the
                # same turn again; never treat its prior content as an answer.
                last_result_hash = None
                stable_result_polls = 0
                await self._wait_job_poll(job.id, poll_seconds)
                continue
            if snapshot.state == "SEND_PENDING":
                job.state = "SEND_PENDING"
                job.progress = snapshot.progress or "仍在等待原页面建立会话；未重复发送。"
                self.store.save(job)
                await self._wait_job_poll(job.id, poll_seconds)
                continue
            if snapshot.state == "RUNNING" and not getattr(snapshot, "generation_active", None):
                settled_polls += 1
                if settled_polls in {1, self.settings.idle_resync_polls}:
                    submission_phase("turn_stream_stage", stage="settled_idle",
                                     settled_polls=settled_polls,
                                     poll_seconds=poll_seconds,
                                     assistant_message_id=job.current_assistant_message_id)
            else:
                settled_polls = 0
            if snapshot.state == "RESEARCH_PLAN_READY" or getattr(snapshot, "research_plan_ready", False):
                if job.research_start_state is not None:
                    job.state = "ERROR"
                    job.error_code = "RESEARCH_START_UNCONFIRMED"
                    job.error = "Research start was already requested; do not repeat the potentially consumed action."
                    job.completed_at = utc_now()
                    self.store.save(job)
                    return
                receipt.research_start_state = job.research_start_state = "requested"
                job.research_state = "start_requested"
                self._persist_turn_receipt(job, receipt)
                self.store.save(job)  # Freeze one-use intent before the Web action.
                try:
                    started = await self._call_browser(
                        self.browser.start_research, receipt,
                        cancel_event=self._cancel_events.setdefault(job.id, asyncio.Event()),
                    )
                    # Cancellation can win while the browser is checking the
                    # transition. Preserve its one-use intent and stop the
                    # original run; never report a successful start on None.
                    current = self.store.get(job.id) or job
                    if current.state in {"CANCEL_REQUESTED", "CANCELLED"}:
                        self._persist_turn_receipt(job, receipt)
                        await self._finish_job_cancellation(job, receipt)
                        return
                    if self.settings.provider == "chatgpt" and started is not True:
                        raise BrowserTaskError("The matched research start transition was not confirmed")
                except Exception as exc:
                    self._persist_turn_receipt(job, receipt)
                    self._persist_account_metadata(job, exc)
                    job.state = "ERROR"
                    job.error_code = getattr(exc, "code", "RESEARCH_START_UNCONFIRMED")
                    job.error = str(exc) if job.error_code in {"UPSTREAM_LIMIT", "ACCOUNT_CONTEXT_REQUIRED"} else "Research start could not be confirmed; inspect the original conversation instead of resubmitting."
                    job.completed_at = utc_now()
                    self.store.save(job)
                    return
                receipt.research_start_state = job.research_start_state = "confirmed"
                job.research_state = "running"
                self._persist_turn_receipt(job, receipt)
                job.state = "RUNNING"
                self.store.save(job)
                await self._wait_job_poll(job.id, poll_seconds)
                continue
            if snapshot.state == "COMPLETE" and snapshot.result_text:
                # A quiet outer assistant or stable progress text is not the
                # embedded research report. This also applies when consult or
                # critic actually invokes the research tool.
                if (self.settings.provider == "chatgpt" and receipt.research_expected
                        and not getattr(snapshot, "research_report_complete", False)):
                    last_result_hash = None
                    stable_result_polls = 0
                    job.state = "RUNNING"
                    job.progress = snapshot.progress or "研究尚未确认完成，继续观察原页面中的报告。"
                    self.store.save(job)
                    await self._wait_job_poll(job.id, poll_seconds)
                    continue
                content = snapshot.result_text.strip()
                if job.mode == "research" or job.research_observed:
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
                        await self._wait_job_poll(job.id, poll_seconds)
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
                return
            # Stability observations must be consecutive completed report
            # observations, never separated by more running/unknown output.
            last_result_hash = None
            stable_result_polls = 0
            if snapshot.state == "NEEDS_USER_INPUT":
                job.state = "NEEDS_USER_INPUT"
                job.user_action = snapshot.user_action
                job.user_input_kind = getattr(snapshot, "user_input_kind", None) or "manual_browser"
                # Only the browser's matched, inactive current assistant turn
                # can supply a readable clarification; never a stale result.
                job.clarification_text = getattr(snapshot, "clarification_text", None)
                job.completed_at = utc_now()
                self.store.save(job)
                return
            job.state = "RUNNING"
            self.store.save(job)
            await self._wait_job_poll(job.id, poll_seconds)
        job.state = "TIMEOUT"
        job.completed_at = utc_now()
        budget = self.settings.timeout_seconds(wait_mode)
        active = getattr(last_snapshot, "generation_active", None)
        if job.submission_state == "confirmed":
            research_wait = self.settings.provider == "chatgpt" and receipt.research_expected
            job.error_code = ("RESEARCH_REPORT_WAIT_TIMEOUT" if research_wait else
                              "GENERATION_WAIT_TIMEOUT" if active is True else "TURN_RESULT_WAIT_TIMEOUT")
            settled_unchangeable = active is not True and settled_polls >= self.settings.idle_resync_polls
            submission_phase("turn_stream_unsettled", generation_active=active, settled_polls=settled_polls,
                             settled_unchangeable=settled_unchangeable,
                             assistant_message_id=job.current_assistant_message_id)
            observed = ("网页仍显示当前研究进行中" if active is True else "尚未取得可关联且完成的研究报告") if research_wait else (
                "网页仍显示当前回合进行中" if active is True else
                "页面连续多次无生成信号（无停止按钮、无进行中标记），但未出现可关联的正式回答" if settled_unchangeable else
                "尚未取得可关联的正式助手回答")
            job.error = f"原问题已确认发送；服务 {budget:g} 秒的观察预算已耗尽，{observed}。保留原会话和回执，未停止上游或重新发送。"
        else:
            job.error_code = "SEND_UNCONFIRMED"
            job.error = f"服务 {budget:g} 秒的观察预算已耗尽，原发送仍未确认。保留原任务与页面；不能据此断定未发送，勿重复提交。"
        submission_phase("generation_wait_timeout", budget_seconds=budget,
            generation_active=active, submission_state=job.submission_state,
            user_message_id=job.current_user_message_id, assistant_message_id=job.current_assistant_message_id,
            error_code=job.error_code)
        self.store.save(job)

    async def _recover_runtime_error(self, job: Job, receipt: SubmitReceipt, snapshot) -> bool:
        """Recover one observed runtime failure without creating another turn."""
        current = self.store.get(job.id) or job
        job.runtime_recovery_attempts = current.runtime_recovery_attempts
        job.runtime_recovery_actions = list(current.runtime_recovery_actions)

        async def cancelled() -> bool:
            latest = self.store.get(job.id) or job
            if latest.state == "CANCELLED":
                return True
            if latest.state == "CANCEL_REQUESTED":
                job.cancel_requested_at = latest.cancel_requested_at
                await self._finish_job_cancellation(job, receipt)
                return True
            return False

        def failed(code: str, message: str) -> bool:
            self._persist_turn_receipt(job, receipt)
            job.state = "ERROR"
            job.error_code = code
            job.error = message
            job.user_action = None
            job.user_input_kind = None
            job.clarification_text = None
            job.progress = None
            job.completed_at = utc_now()
            self.store.save(job)
            return False

        if await cancelled():
            return False
        if job.runtime_recovery_attempts >= MAX_RUNTIME_RECOVERY_ACTIONS:
            return failed("RUNTIME_RECOVERY_EXHAUSTED",
                          "The same turn's two runtime recovery actions are exhausted; the original request was not resent.")
        recover = getattr(self.browser, "recover_runtime_error", None)
        if not callable(recover):
            return failed("RUNTIME_RECOVERY_UNSUPPORTED",
                          "The browser adapter cannot safely recover this runtime error; the original request was not resent.")
        reserved = False

        async def prepared(action: str) -> None:
            nonlocal reserved
            latest = self.store.get(job.id) or job
            if latest.state in {"CANCEL_REQUESTED", "CANCELLED"}:
                raise _RuntimeRecoveryCancelled()
            if reserved:
                raise BrowserTaskError("A runtime recovery operation attempted more than one browser action.")
            if latest.runtime_recovery_attempts >= MAX_RUNTIME_RECOVERY_ACTIONS:
                raise BrowserTaskError("The durable runtime recovery budget is exhausted.")
            if not isinstance(action, str) or not action.strip():
                raise BrowserTaskError("A runtime recovery action has no observable name.")
            job.runtime_recovery_attempts = latest.runtime_recovery_attempts + 1
            receipt.runtime_recovery_attempts = job.runtime_recovery_attempts
            job.runtime_recovery_actions = [*latest.runtime_recovery_actions, {
                "attempt": job.runtime_recovery_attempts,
                "action": action,
                "prepared_at": utc_now(),
                "runtime_error_code": getattr(snapshot, "runtime_error_code", None),
            }]
            self._persist_turn_receipt(job, receipt)
            job.state = "RUNNING"
            job.progress = f"正在恢复当前网页运行错误（{job.runtime_recovery_attempts}/{MAX_RUNTIME_RECOVERY_ACTIONS}）；保留原问题与回执。"
            job.error = None
            job.error_code = None
            job.user_action = None
            job.user_input_kind = None
            self.store.save(job)
            reserved = True
            submission_phase("runtime_recovery_prepared", action=action,
                             attempt=job.runtime_recovery_attempts,
                             runtime_error_code=getattr(snapshot, "runtime_error_code", None))

        try:
            attempted = await self._call_browser(
                recover, receipt, attempt=job.runtime_recovery_attempts + 1,
                on_recovery_prepared=prepared, timeout=self.settings.browser_timeout_seconds,
                cancel_event=self._cancel_events.setdefault(job.id, asyncio.Event()),
            )
        except _RuntimeRecoveryCancelled:
            await cancelled()
            return False
        except BrowserNeedsUserAction as exc:
            if await cancelled():
                return False
            reason = str(getattr(exc, "reason", "")).casefold()
            code = str(getattr(exc, "code", "") or "").upper()
            if reason in {"login_required", "verification_required", "captcha_required"} or code in {
                "LOGIN_REQUIRED", "VERIFICATION_REQUIRED", "CAPTCHA_REQUIRED",
            }:
                self._persist_turn_receipt(job, receipt)
                job.state = "NEEDS_USER_INPUT"
                job.user_action = exc.instructions
                job.user_input_kind = getattr(exc, "user_input_kind", "manual_browser")
                job.error = exc.reason
                job.error_code = getattr(exc, "code", None)
                job.completed_at = utc_now()
                self.store.save(job)
                return False
            return failed("RUNTIME_RECOVERY_UNSAFE", str(exc))
        except Exception as exc:
            if await cancelled():
                return False
            logger.exception("task %s failed during runtime recovery", job.id)
            return failed(getattr(exc, "code", None) or "RUNTIME_RECOVERY_FAILED", str(exc))
        if await cancelled():
            return False
        if attempted is True and not reserved:
            return failed("RUNTIME_RECOVERY_UNCONFIRMED",
                          "The browser reported an action without its durable reservation; no further recovery action was attempted.")
        if attempted is False and reserved:
            return failed("RUNTIME_RECOVERY_UNCONFIRMED",
                          "A runtime action was reserved but its outcome is unknown; no further recovery action was attempted.")
        if attempted is not True and attempted is not False:
            return failed("RUNTIME_RECOVERY_UNCONFIRMED", "The browser returned no verified runtime recovery outcome.")
        self._persist_turn_receipt(job, receipt)
        job.state = "RUNNING"
        job.error = None
        job.error_code = None
        job.progress = ("网页恢复阶段已完成，继续观察原问题。" if attempted
                        else "页面状态已变化，继续观察原问题。")
        self.store.save(job)
        return True

    @staticmethod
    def _persist_account_metadata(job: Job, source) -> None:
        for name in ("account_index", "account_attempts", "account_switch"):
            if hasattr(source, name):
                value = getattr(source, name)
                setattr(job, name, list(value) if isinstance(value, list) else value)

    @staticmethod
    def _remember_research_observation(job: Job, receipt: SubmitReceipt, snapshot) -> None:
        previous = (job.research_observed, job.research_state, job.research_report_complete)
        observed = bool(getattr(snapshot, "research_observed", False)
                        or getattr(snapshot, "research_plan_ready", False)
                        or snapshot.state == "RESEARCH_PLAN_READY")
        if observed:
            receipt.research_observed = job.research_observed = True
            receipt.research_expected = job.research_expected = True
        state = getattr(snapshot, "research_state", None)
        if state:
            receipt.research_state = job.research_state = state
        for name in ("research_run_id", "research_report_id"):
            value = getattr(snapshot, name, None)
            if value:
                setattr(job, name, value)
                setattr(receipt, name, value)
        # This is current evidence, not a latch. A later active or unknown
        # observation invalidates a previously readable report snapshot.
        if observed or state or receipt.research_expected:
            job.research_report_complete = bool(getattr(snapshot, "research_report_complete", False))
        if previous != (job.research_observed, job.research_state, job.research_report_complete):
            submission_phase("research_observation", research_observed=job.research_observed,
                             research_state=job.research_state,
                             report_complete=job.research_report_complete,
                             run_id=job.research_run_id, report_id=job.research_report_id)

    @staticmethod
    def _persist_turn_receipt(job: Job, receipt: SubmitReceipt) -> None:
        for name in (
            "user_message_ids_before", "assistant_message_ids_before",
            "submitted_prompt_sha256", "current_user_message_id",
            "current_assistant_message_id", "submission_state", "submission_verified_at",
            "research_expected", "research_start_state",
            "research_user_message_id", "research_user_prompt_sha256", "research_assistant_message_id",
            "research_observed", "research_state",
            "research_run_id", "research_report_id",
            "provisional_uncommitted_since",
            "account_index", "account_attempts", "account_switch",
        ):
            value = getattr(receipt, name)
            setattr(job, name, list(value) if isinstance(value, list) else value)

    def _conversation_key(self, url: str | None) -> str | None:
        if not url:
            return None
        try:
            canonical = self.policy.canonical_url(url)
            if self.settings.provider == "chatgpt":
                return urlparse(canonical).path
            return f"gemini:{gemini_account_index(canonical)}:{urlparse(canonical).path.split('/')[-1]}"
        except ValueError:
            return None

    def _conversation_jobs(self, url: str | None) -> list[Job]:
        key = self._conversation_key(url)
        return [item for item in self.store.all() if key and self._conversation_key(item.conversation_url) == key]

    def _bootstrap_job(self, url: str | None) -> Job | None:
        return next((item for item in self._conversation_jobs(url) if confirmed_bootstrap(item)), None)

    def _origin_receipts(self, url: str) -> dict[str, dict[str, Any]]:
        return {
            item.current_user_message_id: {
                "task_id": item.id, "request_id": item.request_id,
                "user_message_id": item.current_user_message_id,
                "submission_verified_at": item.submission_verified_at,
                "submitted_prompt_sha256": item.submitted_prompt_sha256,
                "protocol_version": item.oracle_composition_version,
                "bootstrap_included": item.oracle_bootstrap_included,
                "mode": item.mode,
            }
            for item in self._conversation_jobs(url)
            if item.submission_state == "confirmed" and item.current_user_message_id and item.submitted_prompt_sha256
        }

    def public_session(self, session: ConversationSession, *, tab_open: bool | None = None) -> dict[str, Any]:
        value = session.public(tab_open=tab_open)
        source = self._bootstrap_job(session.conversation_url)
        if source:
            value["oracle_protocol"]["bootstrap"] = public_protocol(source)["bootstrap"]
        return value

    async def _freeze_sending_text(self, job: Job) -> None:
        """Called in the Session FIFO, before uploads or any send mutation."""
        if job.sending_text is not None:
            # Capacity deferral and same-task recovery never recompose against
            # a newer Session state or newly installed resource.
            if hashlib.sha256(job.sending_text.encode("utf-8")).hexdigest() != job.sending_text_sha256:
                raise BrowserTaskError("Frozen sending text failed its integrity check; no message was sent.")
            return
        first = not job.conversation_url
        source = self._bootstrap_job(job.conversation_url)
        prior = [item for item in self._conversation_jobs(job.conversation_url)
                 if item.id != job.id and item.submission_state == "confirmed" and item.current_user_message_id]
        last = prior[-1] if prior else None
        reminder = bool(source and job.oracle_recovery_pending)
        has_tab = getattr(self.browser, "has_conversation_tab", None)
        if source and not reminder and has_tab is not None:
            reminder = not await self._call_browser(has_tab, job.conversation_url, timeout=min(5.0, self.settings.browser_timeout_seconds))
        job.oracle_bootstrap_included = first
        job.oracle_mode_overlay_included = first or last is None or last.mode != job.mode
        job.oracle_recovery_reminder_included = reminder
        job.oracle_bootstrap_version = BOOTSTRAP_VERSION if first else source.oracle_bootstrap_version if source else None
        if source:
            job.oracle_bootstrap_task_id = source.id
            job.oracle_bootstrap_user_message_id = source.current_user_message_id
            job.oracle_bootstrap_verified_at = source.submission_verified_at
        job.sending_text = compose_message(
            job.prompt, job.mode, include_bootstrap=first,
            include_mode_overlay=job.oracle_mode_overlay_included, recovery_reminder=reminder,
        )
        job.sending_text_sha256 = hashlib.sha256(job.sending_text.encode("utf-8")).hexdigest()
        job.sending_text_normalized_sha256 = normalized_text_sha256(job.sending_text)
        job.oracle_composition_version = PROTOCOL_VERSION
        job.oracle_prepared_at = utc_now()
        current = self.store.get(job.id)
        if current and current.state in {"CANCEL_REQUESTED", "CANCELLED"}:
            job.state = current.state
            job.cancel_requested_at = current.cancel_requested_at
        self.store.save(job)

    async def _wait_job_poll(self, task_id: str, seconds: float) -> None:
        event = self._cancel_events.setdefault(task_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _finish_job_cancellation(self, job: Job, receipt: SubmitReceipt, *, snapshot=None) -> None:
        """Stop independently of answer correlation; never wait the answer deadline."""
        job.cancel_requested_at = job.cancel_requested_at or utc_now()
        stop = getattr(self.browser, "request_stop", None)
        stop_verified = False
        if stop is not None:
            try:
                stop_verified = await self._call_browser(
                    stop, receipt, timeout=min(15.0, self.settings.browser_timeout_seconds),
                ) is True
            except (BrowserUnresponsiveError, ResourceLockTimeout, PlaywrightError) as exc:
                job.error = f"Generation stop could not be verified: {type(exc).__name__}."
        else:
            if snapshot is None:
                try:
                    snapshot = await self._call_browser(
                        self.browser.inspect, receipt, timeout=min(15.0, self.settings.browser_timeout_seconds),
                    )
                except (BrowserUnresponsiveError, ResourceLockTimeout, PlaywrightError) as exc:
                    job.error = f"Generation stop could not be verified: {type(exc).__name__}."
            generation_active = getattr(snapshot, "generation_active", None)
            stop_verified = generation_active is False or (
                generation_active is None and getattr(snapshot, "state", None) in {"COMPLETE", "NEEDS_USER_INPUT"}
            )
        self._persist_turn_receipt(job, receipt)
        job.last_checked_at = utc_now()
        job.cancellation_stop_verified = stop_verified
        if not stop_verified:
            # Detach only this Session; a closed page is not proof cloud generation stopped.
            if job.session_id:
                session = self.sessions.get(job.session_id)
                if session is not None:
                    for waiting_id in self.scheduler.cancel_waiting_session_tasks(session.id):
                        waiting_job = self.store.get(waiting_id)
                        if waiting_job is not None:
                            await self._admission_cancelled(waiting_job)
                    self.sessions.close(session.id)
                    session = self.sessions.get(session.id)
                    await self._close_session_pages(session)
            else:
                close = getattr(self.browser, "close_owned_pages", None)
                if close is not None:
                    await self._call_browser(close, keep_for_user=False, retain_key=None)
            job.error_code = "CANCEL_STOP_UNCONFIRMED"
            job.error = f"Local operation cancelled and its page released; {self.policy.title} generation stop was not verified. Do not resend the original prompt."
            job.progress = "本地任务已取消并回收自身页面；网页生成是否停止未确认。"
        else:
            job.error_code = None
            job.error = None
            job.progress = "ChatGPT 页面已停止，任务已取消。"
        job.state = "CANCELLED"
        job.completed_at = utc_now()
        self.store.save(job)

    def public(self, task_id: str) -> dict[str, Any]:
        job = self.get(task_id)
        value = {**public_job(job), "scheduler": self.scheduler.status(task_id)}
        if self.settings.provider == "gemini":
            value.update(provider="gemini", selection_evidence={
                "kind": "visible_picker" if job.selection_verified_at else "unverified",
                "generation_model": "unknown",
            })
            if job.research_expected:
                value["research"] = {"start_state": job.research_start_state,
                                     "provider_user_message_id": job.research_user_message_id,
                                     "assistant_message_id": job.research_assistant_message_id}
            for key in ("progress",):
                if value.get(key):
                    value[key] = value[key].replace("ChatGPT", "Gemini").replace("chatgpt_", "gemini_")
        return value

    def _requires_turn_receipts(self) -> bool:
        return isinstance(self.browser, ChatGPTBrowser) or bool(getattr(self.browser, "requires_turn_receipts", False))

    def _remember_submission_receipt(self, job: Job, receipt: SubmitReceipt) -> None:
        self._receipts[job.id] = receipt
        # Home and local-chatgpt URLs are useful diagnostic observations, but
        # cannot become a durable conversation mapping or an automatic resume.
        try:
            canonical = self.policy.canonical_url(receipt.conversation_url)
            if self.settings.provider == "chatgpt" and "local-chatgpt:" in unquote(urlparse(canonical).path):
                canonical = None
        except (ValueError, TypeError, AttributeError):
            canonical = None
        if canonical:
            job.conversation_url = canonical
            if job.session_id:
                self.sessions.touch(job.session_id, conversation_url=canonical)
        job.assistant_count_before = receipt.assistant_count_before
        self._persist_turn_receipt(job, receipt)
        job.resolved_model = receipt.resolved_model
        job.resolved_reasoning_effort = receipt.resolved_reasoning_effort
        explicit_selection = bool(job.model or job.reasoning_effort or job.quality != "auto")
        job.selection_source = receipt.selection_source if explicit_selection else "default"
        job.selection_verified_at = receipt.selection_verified_at
        if receipt.attachments:
            job.attachments = receipt.attachments

    def _prepared_submission_options(self, job: Job, method) -> dict[str, Any]:
        try:
            if "on_submission_prepared" not in inspect.signature(method).parameters:
                return {}
        except (ValueError, TypeError):
            return {}

        async def prepared(receipt: SubmitReceipt) -> None:
            current = self.store.get(job.id) or job
            if current.state in {"CANCEL_REQUESTED", "CANCELLED"}:
                raise UploadError("Task cancelled before the send attempt")
            self._remember_submission_receipt(job, receipt)
            job.state = "SEND_PENDING"
            job.progress = "发送前回执已保存；本轮发送尚未确认。"
            self.store.save(job)
            submission_phase("receipt_prepared", observed_page_url=receipt.conversation_url,
                             submitted_prompt_sha256=receipt.submitted_prompt_sha256)

        return {"on_submission_prepared": prepared}

    def _continuation_options(self, job: Job) -> dict[str, Any]:
        return {"mode": job.mode} if getattr(self.browser, "continuation_accepts_mode", False) else {}

    def _dedicated_submit_options(self, job: Job) -> dict[str, Any]:
        dedicated = bool(job.session_id or self._requires_turn_receipts())
        if not dedicated:
            return {}
        try:
            parameters = inspect.signature(self.browser.submit).parameters.values()
            if not any(parameter.name == "dedicated_page" or parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
                return {}
        except (TypeError, ValueError):
            return {"dedicated_page": True}
        return {"dedicated_page": True}
