from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from chatgpt_oracle.config import Settings
from chatgpt_oracle.browser import StreamedAttachment
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.server import AuthHealthMiddleware

from test_export_http import HttpExportBrowser


class StreamingAttachmentBrowser(HttpExportBrowser):
    async def export_conversation(self, url: str, **_: object) -> dict[str, object]:
        return {
            "messages": [{"id": "m1", "role": "user", "text": "inspect this image"}],
            "attachments": [
                {
                    "attachment_id": "file_123",
                    "message_id": "m1",
                    "filename": "diagram.png",
                    "mime_type": "image/png",
                    "file_kind": "image_asset_pointer",
                    "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_123",
                    "downloadable": True,
                }
            ],
        }

    async def download_attachment(
        self,
        url: str,
        *,
        attachment_id: str | None = None,
        conversation_url: str | None = None,
        destination: Path | None = None,
        max_file_bytes: int | None = None,
        on_progress: Any = None,
    ) -> StreamedAttachment:
        data = b"verified streamed image bytes"
        assert destination is not None
        assert len(data) <= (max_file_bytes or 0)
        destination.write_bytes(data)
        if on_progress:
            await on_progress(len(data))
        return StreamedAttachment(len(data), "image/png", None)


async def _request_bytes(
    app: Any,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    chunks: list[bytes] | None = None,
    authorization: str | None = "Bearer test-token",
) -> tuple[int, bytes, dict[str, str]]:
    parsed = urlsplit(path)
    body_chunks = list(chunks or [b""])
    events = iter([
        {"type": "http.request", "body": chunk, "more_body": index + 1 < len(body_chunks)}
        for index, chunk in enumerate(body_chunks)
    ])
    sent: list[dict[str, Any]] = []
    request_headers = [(key.lower().encode("ascii"), value.encode("latin-1")) for key, value in (headers or {}).items()]
    if authorization:
        request_headers.append((b"authorization", authorization.encode("ascii")))

    async def receive() -> dict[str, Any]:
        return next(events, {"type": "http.disconnect"})

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": parsed.path,
            "query_string": parsed.query.encode("ascii"),
            "headers": request_headers,
            "client": ("127.0.0.1", 12345),
        },
        receive,
        send,
    )
    response = next(item for item in sent if item["type"] == "http.response.start")
    response_headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in response["headers"]}
    response_body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return int(response["status"]), response_body, response_headers


async def _stream_response_digest(
    app: Any,
    *,
    path: str,
    headers: dict[str, str],
    consumer_delay_seconds: float = 0,
) -> tuple[int, dict[str, str], int, int, str]:
    parsed = urlsplit(path)
    response_start: dict[str, Any] | None = None
    total_bytes = 0
    max_block_bytes = 0
    digest = hashlib.sha256()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal response_start, total_bytes, max_block_bytes
        if message["type"] == "http.response.start":
            response_start = message
        elif message["type"] == "http.response.body":
            block = message.get("body", b"")
            total_bytes += len(block)
            max_block_bytes = max(max_block_bytes, len(block))
            digest.update(block)
            if consumer_delay_seconds:
                await asyncio.sleep(consumer_delay_seconds)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": parsed.path,
            "query_string": parsed.query.encode("ascii"),
            "headers": [(key.lower().encode("ascii"), value.encode("latin-1")) for key, value in headers.items()],
            "client": ("127.0.0.1", 12345),
        },
        receive,
        send,
    )
    assert response_start is not None
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in response_start["headers"]
    }
    return int(response_start["status"]), response_headers, total_bytes, max_block_bytes, digest.hexdigest()


def _manager(tmp_path: Path) -> tuple[JobManager, AuthHealthMiddleware]:
    settings = replace(
        Settings.from_env(tmp_path),
        jobs_dir=tmp_path / "jobs",
        browser_profile_dir=tmp_path / "profile",
        sessions_dir=tmp_path / "sessions",
        file_store_dir=tmp_path / "file-store",
        file_store_max_file_bytes=1024,
        file_store_max_total_bytes=4096,
        file_store_max_chunk_bytes=8,
        file_store_io_chunk_bytes=4,
    )
    manager = JobManager(settings, HttpExportBrowser())
    return manager, AuthHealthMiddleware(lambda *_: None, "test-token", manager.uploads, manager)


def _metadata(**values: str) -> str:
    return ",".join(
        f"{key} {base64.b64encode(value.encode('utf-8')).decode('ascii')}"
        for key, value in values.items()
    )


def test_tus_upload_resume_and_repeatable_range_download(tmp_path: Path) -> None:
    async def run() -> None:
        manager, app = _manager(tmp_path)
        restarted_manager, restarted_app = _manager(tmp_path)
        try:
            manager.sessions.create("agent-a")
            status, _, options = await _request_bytes(app, method="OPTIONS", path="/v2/uploads")
            assert status == 204
            assert options["tus-version"] == "1.0.0"
            assert set(options["tus-extension"].split(",")) == {"creation", "termination"}

            content = b"0123456789abcdef"
            digest = hashlib.sha256(content).hexdigest()
            metadata = _metadata(
                session_id="agent-a",
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=str(len(content)),
                sha256=digest,
            )
            status, _, created = await _request_bytes(
                app,
                method="POST",
                path="/v2/uploads",
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Length": str(len(content)),
                    "Upload-Metadata": metadata,
                    "X-Oracle-Session-ID": "agent-a",
                    "Content-Length": "0",
                },
            )
            assert status == 201
            upload_url = created["location"]
            assert created["upload-offset"] == "0"

            status, _, head = await _request_bytes(
                app,
                method="HEAD",
                path=upload_url,
                headers={"Tus-Resumable": "1.0.0", "X-Oracle-Session-ID": "agent-a"},
            )
            assert status == 200
            assert head["upload-offset"] == "0"

            status, _, first = await _request_bytes(
                app,
                method="PATCH",
                path=upload_url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "X-Oracle-Session-ID": "agent-a",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "0",
                    "Content-Length": "8",
                },
                chunks=[content[:3], content[3:8]],
            )
            assert status == 204
            assert first["upload-offset"] == "8"

            status, _, resumed_head = await _request_bytes(
                restarted_app,
                method="HEAD",
                path=upload_url,
                headers={"Tus-Resumable": "1.0.0", "X-Oracle-Session-ID": "agent-a"},
            )
            assert status == 200
            assert resumed_head["upload-offset"] == "8"

            status, _, complete = await _request_bytes(
                restarted_app,
                method="PATCH",
                path=upload_url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "X-Oracle-Session-ID": "agent-a",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "8",
                    "Content-Length": "8",
                },
                chunks=[content[8:]],
            )
            assert status == 204
            file_ref = complete["x-oracle-file-ref"]
            assert complete["x-content-sha256"] == digest

            info = restarted_manager.files.get_file("agent-a", file_ref)
            download = info.public(content_url=f"/v2/files/{file_ref}/content")
            download_url = download["download_url"]
            status, _, file_head = await _request_bytes(
                restarted_app,
                method="HEAD",
                path=download_url,
                headers=download["download_headers"],
            )
            assert status == 200
            assert file_head["content-length"] == str(len(content))
            assert file_head["etag"] == f'"{digest}"'

            status, piece, ranged = await _request_bytes(
                restarted_app,
                method="GET",
                path=download_url,
                headers={**download["download_headers"], "Range": "bytes=3-8"},
            )
            assert status == 206
            assert piece == content[3:9]
            assert ranged["content-range"] == f"bytes 3-8/{len(content)}"
            assert ranged["x-content-sha256"] == digest

            status, full, _ = await _request_bytes(
                restarted_app, method="GET", path=download_url, headers=download["download_headers"]
            )
            assert status == 200
            assert full == content
            status, full_again, _ = await _request_bytes(
                restarted_app, method="GET", path=download_url, headers=download["download_headers"]
            )
            assert status == 200
            assert full_again == content

            status, _, unsatisfied = await _request_bytes(
                restarted_app,
                method="GET",
                path=download_url,
                headers={**download["download_headers"], "Range": "bytes=100-200"},
            )
            assert status == 416
            assert unsatisfied["content-range"] == f"bytes */{len(content)}"

            status, _, wrong_session = await _request_bytes(
                restarted_app,
                method="GET",
                path=download_url,
                headers={"X-Oracle-Session-ID": "agent-b"},
            )
            assert status == 404
        finally:
            await manager.stop()
            await restarted_manager.stop()

    asyncio.run(run())


def test_tus_rejects_bad_auth_offset_and_checksum(tmp_path: Path) -> None:
    async def run() -> None:
        manager, app = _manager(tmp_path)
        try:
            manager.sessions.create("agent-a")
            status, body, _ = await _request_bytes(
                app,
                method="OPTIONS",
                path="/v2/uploads",
                authorization=None,
            )
            assert status == 401

            content = b"verified"
            metadata = _metadata(session_id="agent-a", filename="notes.txt", sha256="0" * 64)
            status, _, created = await _request_bytes(
                app,
                method="POST",
                path="/v2/uploads",
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Length": str(len(content)),
                    "Upload-Metadata": metadata,
                    "X-Oracle-Session-ID": "agent-a",
                    "Content-Length": "0",
                },
            )
            assert status == 201
            upload_url = created["location"]
            status, message, error = await _request_bytes(
                app,
                method="PATCH",
                path=upload_url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "X-Oracle-Session-ID": "agent-a",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "1",
                    "Content-Length": str(len(content)),
                },
                chunks=[content],
            )
            assert status == 409
            assert error["x-error-code"] == "OFFSET_MISMATCH"

            status, message, error = await _request_bytes(
                app,
                method="PATCH",
                path=upload_url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "X-Oracle-Session-ID": "agent-a",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "0",
                    "Content-Length": str(len(content)),
                },
                chunks=[content],
            )
            assert status == 422
            assert error["x-error-code"] == "CHECKSUM_MISMATCH"
            transfer_id = upload_url.rsplit("/", 1)[-1]
            assert manager.files.transfer_status(transfer_id, "agent-a")["state"] == "FAILED"
        finally:
            await manager.stop()

    asyncio.run(run())


def test_chatgpt_attachment_download_becomes_a_repeatable_file_ref(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            browser_profile_dir=tmp_path / "profile",
            sessions_dir=tmp_path / "sessions",
            file_store_dir=tmp_path / "file-store",
            file_store_max_file_bytes=1024,
            file_store_max_total_bytes=4096,
        )
        manager = JobManager(settings, StreamingAttachmentBrowser())
        try:
            started = await manager.export_start(
                conversation_url="https://chatgpt.com/c/stream-download",
                scope="all",
                request_id="download-source-export",
                on_busy="queue",
            )
            for _ in range(200):
                export_status = await manager.export_status(started["session_id"], started["export_id"])
                if export_status["state"] not in {"QUEUED", "RUNNING"}:
                    break
                await asyncio.sleep(0.01)
            assert export_status["state"] == "READY"

            transfer = await manager.start_attachment_download(
                session_id=started["session_id"],
                export_id=started["export_id"],
                attachment_id="file_123",
                request_id="download-pointer-image",
            )
            assert transfer["state"] == "FETCHING"
            download_task = manager._file_download_tasks[transfer["transfer_id"]]
            for _ in range(200):
                status = manager.files.transfer_status(transfer["transfer_id"], started["session_id"])
                if status["state"] != "FETCHING":
                    break
                await asyncio.sleep(0.01)
            await download_task
            status = manager.files.transfer_status(transfer["transfer_id"], started["session_id"])

            assert status["state"] == "READY"
            file_ref = status["file_ref"]
            record = manager.files.get_file(started["session_id"], file_ref)
            assert record.filename == "diagram.png"
            assert record.representation == "derived"
            assert record.provenance == "chatgpt_attachment"
            assert record.path.read_bytes() == b"verified streamed image bytes"
            assert manager.files.get_file(started["session_id"], file_ref).sha256 == status["sha256"]
            assert manager.scheduler.status()["active_task_id"] is None
        finally:
            await manager.stop()

    asyncio.run(run())


@pytest.mark.skipif(
    os.environ.get("CHATGPT_ORACLE_RUN_PRESSURE_TESTS") != "1",
    reason="set CHATGPT_ORACLE_RUN_PRESSURE_TESTS=1 for the 256 MiB file-stream pressure test",
)
def test_256_mib_tus_upload_and_bounded_full_download(tmp_path: Path) -> None:
    async def run() -> None:
        size = 256 * 1024 * 1024
        chunk_size = 8 * 1024 * 1024
        block = bytes(range(256)) * (chunk_size // 256)
        expected_digest = hashlib.sha256()
        for _ in range(size // chunk_size):
            expected_digest.update(block)
        digest = expected_digest.hexdigest()

        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
            file_store_dir=tmp_path / "file-store",
            bearer_token="test-token",
            file_store_max_file_bytes=512 * 1024 * 1024,
            file_store_max_total_bytes=1024 * 1024 * 1024,
            file_store_max_chunk_bytes=chunk_size,
            file_store_io_chunk_bytes=256 * 1024,
        )
        manager = JobManager(settings, HttpExportBrowser())
        app = AuthHealthMiddleware(lambda *_: None, "test-token", manager.uploads, manager)
        try:
            await manager.start()
            manager.sessions.create("pressure-agent")
            metadata = _metadata(
                session_id="pressure-agent",
                filename="pressure.bin",
                mime_type="application/octet-stream",
                sha256=digest,
                size_bytes=str(size),
            )
            status, _, created = await _request_bytes(
                app,
                method="POST",
                path="/v2/uploads",
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Length": str(size),
                    "Upload-Metadata": metadata,
                    "X-Oracle-Session-ID": "pressure-agent",
                    "Content-Length": "0",
                },
            )
            assert status == 201
            upload_url = created["location"]
            transfer_id = upload_url.rsplit("/", 1)[-1]
            offset = 0
            for _ in range(size // chunk_size):
                status, _, patched = await _request_bytes(
                    app,
                    method="PATCH",
                    path=upload_url,
                    headers={
                        "Tus-Resumable": "1.0.0",
                        "X-Oracle-Session-ID": "pressure-agent",
                        "Content-Type": "application/offset+octet-stream",
                        "Upload-Offset": str(offset),
                        "Content-Length": str(chunk_size),
                    },
                    chunks=[block],
                )
                assert status == 204
                offset += chunk_size
                assert patched["upload-offset"] == str(offset)

            status, _, ready_headers = await _request_bytes(
                app,
                method="HEAD",
                path=upload_url,
                headers={"Tus-Resumable": "1.0.0", "X-Oracle-Session-ID": "pressure-agent"},
            )
            assert status == 200
            file_ref = ready_headers["x-oracle-file-ref"]
            assert ready_headers["x-content-sha256"] == digest

            record = manager.files.get_file("pressure-agent", file_ref)
            assert record.size == size
            assert record.sha256 == digest
            download = record.public(content_url=f"/v2/files/{file_ref}/content")
            status, headers, streamed_size, max_block, streamed_digest = await _stream_response_digest(
                app,
                path=download["download_url"],
                headers={**download["download_headers"], "Authorization": "Bearer test-token"},
                consumer_delay_seconds=0.0001,
            )
            assert status == 200
            assert headers["content-length"] == str(size)
            assert headers["x-content-sha256"] == digest
            assert streamed_size == size
            assert streamed_digest == digest
            assert max_block <= settings.file_store_io_chunk_bytes
            assert manager.files.transfer_status(transfer_id, "pressure-agent")["sha256"] == digest
        finally:
            await manager.stop()

    asyncio.run(run())
