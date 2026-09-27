from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from chatgpt_oracle.browser import BrowserSnapshot, ChatGPTBrowser, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.file_store import FileRecord
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.server import AuthHealthMiddleware
import chatgpt_oracle.server as server_module
from chatgpt_oracle.uploads import InMemoryUpload, UploadError, UploadMetadata, UploadStore


def upload_settings(tmp_path: Path) -> Settings:
    base = Settings.from_env(tmp_path)
    return replace(
        base,
        jobs_dir=tmp_path / "jobs",
        browser_profile_dir=tmp_path / "profile",
        sessions_dir=tmp_path / "sessions",
        consult_poll_seconds=0.01,
        consult_timeout_seconds=1,
        upload_max_file_bytes=32,
        upload_max_total_bytes=64,
        upload_max_files=2,
        upload_ttl_seconds=60,
    )


def test_upload_store_keeps_bytes_in_memory_and_consumes_once(tmp_path: Path) -> None:
    async def run() -> None:
        settings = upload_settings(tmp_path)
        store = UploadStore(settings)
        metadata = await store.put("notes.txt", "text/plain; charset=utf-8", b"hello")
        assert metadata.size == 5
        assert metadata.sha256
        assert not list(tmp_path.rglob("notes.txt"))
        payload = await store.attach([metadata.upload_id], "cgo_test")
        assert payload[0].data == b"hello"
        with_error = False
        try:
            await store.attach([metadata.upload_id], "cgo_other")
        except UploadError:
            with_error = True
        assert with_error
        await store.release_job("cgo_test")
        assert store.reserved_bytes == 0

    asyncio.run(run())


def test_upload_stream_enforces_limits(tmp_path: Path) -> None:
    async def run() -> None:
        store = UploadStore(upload_settings(tmp_path))
        chunks = iter([
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ])

        async def receive() -> dict[str, object]:
            return next(chunks)

        metadata = await store.put_stream("stream.bin", "application/octet-stream", receive)
        assert metadata.size == 6
        too_large = iter([
            {"type": "http.request", "body": b"x" * 33, "more_body": False},
        ])

        async def receive_large() -> dict[str, object]:
            return next(too_large)

        try:
            await store.put_stream("large.bin", None, receive_large)
        except UploadError:
            pass
        else:
            raise AssertionError("oversized stream was accepted")

        disconnected = iter([{"type": "http.disconnect"}])

        async def receive_disconnect() -> dict[str, object]:
            return next(disconnected)

        try:
            await store.put_stream("disconnect.bin", None, receive_disconnect)
        except UploadError:
            pass
        else:
            raise AssertionError("disconnected stream was accepted")
        assert store.reserved_bytes == metadata.size

    asyncio.run(run())


class UploadBrowser:
    def __init__(self) -> None:
        self.received: list[bytes] = []

    async def _accept_uploads(
        self,
        uploads: list[InMemoryUpload],
        callback: object,
    ) -> list[dict[str, object]]:
        self.received = [item.data if hasattr(item, "data") else item.path.read_bytes() for item in uploads]
        receipt = []
        for item in uploads:
            metadata = item.metadata.public() if hasattr(item, "metadata") else item.public()
            filename = item.metadata.filename if hasattr(item, "metadata") else item.filename
            receipt.append({**metadata, "displayed_filename": filename, "verified": True})
        if callback:
            await callback(receipt)  # type: ignore[operator]
        return receipt

    async def submit(self, mode: str, prompt: str, **kwargs: object) -> SubmitReceipt:
        uploads = kwargs.get("attachments") or []
        receipt = await self._accept_uploads(uploads, kwargs.get("on_upload_verified"))  # type: ignore[arg-type]
        return SubmitReceipt(
            "https://chatgpt.com/c/upload",
            0,
            attachments=receipt,
        )

    async def continue_conversation(self, conversation_url: str, prompt: str, **kwargs: object) -> SubmitReceipt:
        uploads = kwargs.get("attachments") or []
        receipt = await self._accept_uploads(uploads, kwargs.get("on_upload_verified"))  # type: ignore[arg-type]
        return SubmitReceipt(conversation_url, 0, attachments=receipt)

    async def inspect(self, receipt: SubmitReceipt) -> BrowserSnapshot:
        return BrowserSnapshot("COMPLETE", receipt.conversation_url, result_text="read attachment")

    async def close(self) -> None:
        return None


def test_job_upload_receipt_and_cleanup(tmp_path: Path) -> None:
    async def run() -> None:
        settings = upload_settings(tmp_path)
        store = UploadStore(settings)
        metadata = await store.put("notes.txt", "text/plain", b"hello")
        browser = UploadBrowser()
        manager = JobManager(settings, browser, store)
        job = await manager.submit("consult", "Read the attachment", upload_ids=[metadata.upload_id])
        for _ in range(100):
            if manager.get(job.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        current = manager.get(job.id)
        assert current.state == "COMPLETE"
        assert browser.received == [b"hello"]
        assert current.attachments[0]["verified"] is True
        assert store.reserved_bytes == 0
        await manager.stop()

    asyncio.run(run())


def test_stored_file_ref_reaches_same_session_job_without_memory_payload(tmp_path: Path) -> None:
    async def wait_complete(manager: JobManager, task_id: str) -> None:
        for _ in range(200):
            if manager.get(task_id).state == "COMPLETE":
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"task did not complete: {task_id}")

    async def run() -> None:
        settings = upload_settings(tmp_path)
        browser = UploadBrowser()
        manager = JobManager(settings, browser)
        try:
            session = manager.sessions.create("stored-upload")
            content = b"large file stays on disk during browser upload"
            transfer = manager.files.create_upload(
                session_id=session.id,
                filename="research-notes.txt",
                size=len(content),
                mime_type="text/plain",
                sha256=hashlib.sha256(content).hexdigest(),
            )
            manager.files.append_chunk(transfer["transfer_id"], session.id, 0, content)
            file_path = manager.files.get_file(session.id, transfer["file_ref"]).path

            job = await manager.submit(
                "consult",
                "Read the submitted attachment",
                session_id=session.id,
                file_refs=[transfer["file_ref"]],
            )
            await wait_complete(manager, job.id)

            assert browser.received == [content]
            assert manager.get(job.id).attachments[0]["file_ref"] == transfer["file_ref"]
            assert manager.files.files_for_task(job.id) == []
            assert file_path.exists()
            assert manager.files.get_file(session.id, transfer["file_ref"]).sha256 == transfer["sha256"]
        finally:
            await manager.stop()

    asyncio.run(run())


def test_mcp_resume_sends_a_ready_file_ref_to_the_same_session(monkeypatch, tmp_path: Path) -> None:
    async def wait_complete(manager: JobManager, task_id: str) -> None:
        for _ in range(200):
            if manager.get(task_id).state == "COMPLETE":
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"task did not complete: {task_id}")

    async def run() -> None:
        settings = upload_settings(tmp_path)
        settings = replace(settings, transport="http")
        browser = UploadBrowser()
        manager = JobManager(settings, browser)
        monkeypatch.setattr(server_module, "settings", settings)
        monkeypatch.setattr(server_module, "manager", manager)
        try:
            session = manager.sessions.create("mcp-file-ref")
            first = await server_module.chatgpt_submit(
                "consult",
                "Start this file review.",
                session_id=session.id,
                request_id="mcp-file-task-start",
            )
            await wait_complete(manager, first["task_id"])

            data = b"resume using the persistent file reference"
            transfer = manager.files.create_upload(
                session_id=session.id,
                filename="follow-up.txt",
                size=len(data),
                mime_type="text/plain",
                sha256=hashlib.sha256(data).hexdigest(),
            )
            manager.files.append_chunk(transfer["transfer_id"], session.id, 0, data)
            resumed = await server_module.chatgpt_resume(
                session.id,
                "Please inspect the newly attached source.",
                request_id="mcp-file-task-resume",
                files=[{"file_ref": transfer["file_ref"]}],
            )
            await wait_complete(manager, resumed["task_id"])

            assert browser.received[-1] == data
            assert manager.get(resumed["task_id"]).attachments[0]["file_ref"] == transfer["file_ref"]
        finally:
            await manager.stop()

    asyncio.run(run())


def test_uploads_reach_submit_continue_and_resume_jobs(tmp_path: Path) -> None:
    async def wait_complete(manager: JobManager, task_id: str) -> None:
        for _ in range(200):
            if manager.get(task_id).state == "COMPLETE":
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"task did not complete: {task_id}")

    async def run() -> None:
        settings = upload_settings(tmp_path)
        store = UploadStore(settings)
        browser = UploadBrowser()
        manager = JobManager(settings, browser, store)
        try:
            first_upload = await store.put("first.txt", "text/plain", b"submit bytes")
            first = await manager.submit(
                "consult",
                "Read the first file",
                session_id="upload-roundtrip",
                upload_ids=[first_upload.upload_id],
            )
            await wait_complete(manager, first.id)
            assert browser.received == [b"submit bytes"]

            continued_upload = await store.put("second.txt", "text/plain", b"continue bytes")
            continued = await manager.continue_task(
                first.id,
                "Read the second file",
                upload_ids=[continued_upload.upload_id],
            )
            await wait_complete(manager, continued.id)
            assert browser.received == [b"continue bytes"]

            resumed_upload = await store.put("third.txt", "text/plain", b"resume bytes")
            resumed = await manager.resume_session(
                "upload-roundtrip",
                "Read the third file",
                upload_ids=[resumed_upload.upload_id],
            )
            await wait_complete(manager, resumed.id)
            assert browser.received == [b"resume bytes"]
            assert manager.get(resumed.id).attachments[0]["sha256"] == resumed_upload.sha256
            assert store.reserved_bytes == 0
        finally:
            await manager.stop()

    asyncio.run(run())


def test_upload_http_endpoint_streams_request_body(tmp_path: Path) -> None:
    async def run() -> None:
        settings = upload_settings(tmp_path)
        store = UploadStore(settings)
        browser = UploadBrowser()
        manager = JobManager(settings, browser, store)

        async def app(scope: dict[str, object], receive: object, send: object) -> None:
            raise AssertionError("upload request should be handled by middleware")

        middleware = AuthHealthMiddleware(app, "secret", store, manager)
        messages = iter([
            {"type": "http.request", "body": b"hello ", "more_body": True},
            {"type": "http.request", "body": b"world", "more_body": False},
        ])
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return next(messages)

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/uploads",
                "query_string": b"",
                "headers": [
                    (b"authorization", b"Bearer secret"),
                    (b"x-upload-filename", b"notes.txt"),
                    (b"content-type", b"text/plain"),
                    (b"content-length", b"11"),
                ],
            },
            receive,
            send,
        )
        assert sent[0]["status"] == 201
        body = json.loads(sent[1]["body"].decode())
        assert body["filename"] == "notes.txt"
        assert body["size"] == 11

        job = await manager.submit("consult", "Read the uploaded document", upload_ids=[body["upload_id"]])
        for _ in range(100):
            if manager.get(job.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        assert browser.received == [b"hello world"]
        assert manager.get(job.id).attachments[0]["verified"] is True
        assert store.reserved_bytes == 0
        await manager.stop()

    asyncio.run(run())


def test_duplicate_filenames_get_distinct_browser_names(tmp_path: Path) -> None:
    settings = upload_settings(tmp_path)
    now = 1.0
    uploads = [
        InMemoryUpload(UploadMetadata("1", "same.txt", "text/plain", 1, "a", now, now + 1), b"a"),
        InMemoryUpload(UploadMetadata("2", "same.txt", "text/plain", 1, "b", now, now + 1), b"b"),
    ]
    assert ChatGPTBrowser(settings)._unique_display_names(uploads) == ["same.txt", "same (2).txt"]
