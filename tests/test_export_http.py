from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from chatgpt_oracle.config import Settings
from chatgpt_oracle.exports import ExportStore, MAX_PAGE_RESPONSE_BYTES
from chatgpt_oracle.jobs import JobManager
import chatgpt_oracle.server as server_module
from chatgpt_oracle.server import AuthHealthMiddleware, _PersistentLogFilter


class HttpExportBrowser:
    async def export_conversation(self, url: str, **_: object) -> dict[str, object]:
        return {
            "source": "fixture",
            "completeness": {
                "messages": "verified",
                "attachments": "verified",
                "overall": "verified",
            },
            "messages": [{"id": "m1", "role": "assistant", "text": "through broker"}],
            "attachments": [],
        }

    async def close(self) -> None:
        return None

    async def start(self) -> None:
        return None

    async def has_conversation_tab(self, url: str) -> bool:
        return False

    async def close_conversation_tabs(self, url: str) -> bool:
        return True

    async def download_attachment(
        self,
        url: str,
        *,
        attachment_id: str | None = None,
        conversation_url: str | None = None,
    ) -> tuple[bytes, str, str]:
        return b"verified zip bytes", "application/zip", 'attachment; filename="result.zip"'


async def _request(
    app: Any,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    authorization: str | None = "Bearer test-token",
    client_ip: str = "127.0.0.1",
    capture_headers: bool = False,
    capture_messages: bool = False,
) -> Any:
    request_body = json.dumps(body or {}).encode("utf-8")
    sent: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [(b"content-type", b"application/json")]
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "client": (client_ip, 12345),
        "query_string": b"",
    }
    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    payload = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    content_type = dict(start["headers"]).get(b"content-type", b"")
    if content_type.startswith(b"application/json"):
        result: Any = json.loads(payload)
    else:
        result = payload
    if capture_headers:
        response_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in start["headers"]
        }
        if capture_messages:
            return int(start["status"]), result, response_headers, sent
        return int(start["status"]), result, response_headers
    return int(start["status"]), result


def test_internal_forward_and_public_paging_are_authenticated_and_session_bound(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
            bearer_token="test-token",
            file_store_io_chunk_bytes=4,
        )
        manager = JobManager(settings, HttpExportBrowser())
        app = AuthHealthMiddleware(lambda *_: None, "test-token", manager.uploads, manager)

        status, unauthorized = await _request(
            app,
            method="POST",
            path="/internal/exports/start",
            body={"conversation_url": "https://chatgpt.com/c/http-test"},
            authorization=None,
        )
        assert status == 401
        assert unauthorized["error"] == "unauthorized"

        status, forbidden = await _request(
            app,
            method="POST",
            path="/internal/exports/start",
            body={"conversation_url": "https://chatgpt.com/c/http-test"},
            client_ip="192.0.2.5",
        )
        assert status == 404
        assert forbidden["error_code"] == "NOT_FOUND"

        status, started = await _request(
            app,
            method="POST",
            path="/internal/exports/start",
            body={"conversation_url": "https://chatgpt.com/c/http-test", "on_busy": "queue"},
        )
        assert status == 200
        assert started["state"] == "QUEUED"

        for _ in range(100):
            ready = await manager.export_status(started["session_id"], started["export_id"])
            if ready["state"] == "READY":
                break
            await asyncio.sleep(0.01)
        assert ready["state"] == "READY"

        status, cancel_ready = await _request(
            app,
            method="POST",
            path=f"/exports/{started['export_id']}/cancel",
            body={"session_id": started["session_id"]},
            client_ip="203.0.113.7",
        )
        assert status == 200
        assert cancel_ready["error_code"] == "EXPORT_ALREADY_READY"
        assert manager.scheduler.get_export(started["export_id"])["state"] == "READY"

        status, page = await _request(
            app,
            method="POST",
            path=f"/exports/{started['export_id']}/page",
            body={"session_id": started["session_id"], "view": "messages", "cursor": None},
            client_ip="203.0.113.7",
        )
        assert status == 200
        assert [message["text"] for message in page["messages"]] == ["through broker"]
        assert page["message_segment"] is None

        status, archive, archive_headers, archive_messages = await _request(
            app,
            method="POST",
            path=f"/exports/{started['export_id']}/file",
            body={"session_id": started["session_id"], "view": "markdown"},
            client_ip="203.0.113.7",
            capture_headers=True,
            capture_messages=True,
        )
        assert status == 200
        assert b"through broker" in archive
        assert archive_headers["content-length"] == str(len(archive))
        assert archive_headers["x-content-sha256"] == hashlib.sha256(archive).hexdigest()
        body_chunks = [item.get("body", b"") for item in archive_messages if item["type"] == "http.response.body"]
        assert b"".join(body_chunks) == archive
        assert max(map(len, body_chunks)) <= settings.file_store_io_chunk_bytes

        served_event = manager.scheduler.export_events(started["export_id"])[-1]
        assert served_event["event"] == "VIEW_FILE_SERVED"
        assert served_event["metadata"]["bytes"] == len(archive)
        assert served_event["metadata"]["sha256"] == hashlib.sha256(archive).hexdigest()

        status, archive_again = await _request(
            app,
            method="POST",
            path=f"/exports/{started['export_id']}/file",
            body={"session_id": started["session_id"], "view": "markdown"},
            client_ip="203.0.113.7",
        )
        assert status == 200
        assert archive_again == archive

        status, mismatch = await _request(
            app,
            method="POST",
            path=f"/exports/{started['export_id']}/page",
            body={"session_id": "other-session", "view": "messages", "cursor": None},
            client_ip="203.0.113.7",
        )
        assert status == 200
        assert mismatch["error_code"] == "EXPORT_SESSION_MISMATCH"

        status, session_status = await _request(
            app,
            method="POST",
            path="/internal/exports/session-status",
            body={"session_id": started["session_id"]},
        )
        assert status == 200
        assert session_status["state"] == "LIVE"
        status, closed = await _request(
            app,
            method="POST",
            path="/internal/exports/session-close",
            body={"session_id": started["session_id"]},
        )
        assert status == 200
        assert closed["state"] == "DEAD"
        assert manager.scheduler.get_export(started["export_id"])["state"] == "RELEASED"
        await manager.stop()

    asyncio.run(run())


def test_export_cannot_mark_unavailable_manifest_attachments_verified(tmp_path: Path) -> None:
    class UnavailableAttachmentBrowser(HttpExportBrowser):
        async def export_conversation(self, url: str, **_: object) -> dict[str, object]:
            return {
                "source": "fixture",
                "completeness": {
                    "messages": "verified",
                    "attachments": "verified",
                    "overall": "verified",
                },
                "messages": [{"id": "m1", "role": "user", "text": "file"}],
                "attachments": [{
                    "attachment_id": "library-ref",
                    "library_file_id": "library-ref",
                    "file_kind": "library_file",
                    "message_id": "m1",
                }],
            }

    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
        )
        manager = JobManager(settings, UnavailableAttachmentBrowser())
        started = await manager.export_start(
            conversation_url="https://chatgpt.com/c/unavailable-attachment",
            scope="all",
            on_busy="queue",
        )
        for _ in range(100):
            status = await manager.export_status(started["session_id"], started["export_id"])
            if status["state"] == "READY":
                break
            await asyncio.sleep(0.01)
        assert status["state"] == "READY"
        assert status["completeness"]["messages"] == "verified"
        assert status["completeness"]["attachments"] == "partial"
        assert status["completeness"]["overall"] == "partial"
        assert status["reason"]["attachments"] == "attachment_bytes_unavailable"
        await manager.stop()

    asyncio.run(run())


def test_status_dispatch_starts_worker_for_a_persisted_export(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
            bearer_token="test-token",
        )
        manager = JobManager(settings, HttpExportBrowser())
        app = AuthHealthMiddleware(lambda *_: None, "test-token", manager.uploads, manager)
        manager.scheduler.enqueue_export(
            "exp_recovered",
            session_id="agent-a",
            conversation_url="https://chatgpt.com/c/recovered",
            scope="all",
            count=10,
            request_key=None,
            input_fingerprint="fingerprint-recovered",
            on_busy="queue",
            retry_after_seconds=180,
        )

        status, initial = await _request(
            app,
            method="POST",
            path="/internal/exports/status",
            body={"session_id": "agent-a", "export_id": "exp_recovered"},
        )
        assert status == 200
        assert initial["state"] in {"QUEUED", "RUNNING", "READY"}

        for _ in range(100):
            current = await manager.export_status("agent-a", "exp_recovered")
            if current["state"] == "READY":
                break
            await asyncio.sleep(0.01)
        assert current["state"] == "READY"
        await manager.stop()

    asyncio.run(run())


def test_attachment_download_returns_digest_and_persists_transfer_audit(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
            bearer_token="test-token",
        )
        manager = JobManager(settings, HttpExportBrowser())
        app = AuthHealthMiddleware(lambda *_: None, "test-token", manager.uploads, manager)
        handle = manager.exports.add_handle(
            "exp-file-transfer",
            "att-1",
            "https://chatgpt.com/files/result.zip",
        )

        status, body, headers = await _request(
            app,
            method="GET",
            path=handle.public_url(),
            capture_headers=True,
        )

        digest = hashlib.sha256(body).hexdigest()
        assert status == 200
        assert body == b"verified zip bytes"
        assert headers["content-length"] == str(len(body))
        assert headers["x-content-sha256"] == digest
        event = manager.scheduler.export_events("exp-file-transfer")[-1]
        assert event["event"] == "ATTACHMENT_DOWNLOADED"
        assert event["metadata"]["bytes"] == len(body)
        assert event["metadata"]["sha256"] == digest
        await manager.stop()

    asyncio.run(run())


def test_persistent_log_filter_redacts_urls_and_auth_material() -> None:
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        "server.py",
        1,
        "Bearer secret https://chatgpt.com/c/private?sig=signed Cookie=browser-secret",
        (),
        None,
    )

    assert _PersistentLogFilter().filter(record) is True
    rendered = record.getMessage()
    assert "secret" not in rendered
    assert "signed" not in rendered
    assert "browser-secret" not in rendered
    assert "[URL]" in rendered


def test_stdio_export_proxy_uses_local_broker_and_bearer(monkeypatch: Any, tmp_path: Path) -> None:
    received: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization")
            length = int(self.headers.get("Content-Length", "0"))
            received["body"] = json.loads(self.rfile.read(length))
            response = json.dumps({"accepted": True, "state": "READY"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            server_module,
            "settings",
            replace(Settings.from_env(tmp_path), port=server.server_port, bearer_token="fixture-token"),
        )
        result = server_module._export_proxy_sync("status", {"session_id": "agent-a", "export_id": "exp-a"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result == {"accepted": True, "state": "READY"}
    assert received["path"] == "/internal/exports/status"
    assert received["authorization"] == "Bearer fixture-token"
    assert received["body"] == {"session_id": "agent-a", "export_id": "exp-a"}


def test_structured_message_page_fits_streamable_http_envelope(monkeypatch: Any) -> None:
    async def run() -> None:
        store = ExportStore(ttl_seconds=60)
        text = ('"\\\n' * 3000)
        payload = {
            "manifest": {
                "messages": [{"id": "large-message", "role": "assistant", "text": text}],
                "attachments": [],
            },
            "attachments": [],
            "markdown": text,
        }
        store.put_snapshot(
            "mcp-page",
            session_id="session-a",
            conversation_url="https://chatgpt.com/c/mcp-page",
            payload=payload,
            source="fixture",
            completeness={"messages": "verified", "attachments": "unverified"},
        )

        async def export_call(action: str, request: dict[str, Any]) -> dict[str, Any]:
            assert action == "page"
            return store.page(
                session_id=str(request["session_id"]),
                export_id=str(request["export_id"]),
                view=str(request.get("view") or "messages"),
                cursor=request.get("cursor"),
                max_response_bytes=int(request.get("max_response_bytes") or MAX_PAGE_RESPONSE_BYTES),
            )

        monkeypatch.setattr(server_module, "_export_call", export_call)
        app = server_module.mcp.streamable_http_app()
        wire_lengths: list[int] = []

        async def capture_page_response(response: Any) -> None:
            try:
                request_body = json.loads(response.request.content)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return
            params = request_body.get("params", {})
            if request_body.get("method") == "tools/call" and params.get("name") == "chatgpt_export_page":
                wire_lengths.append(len(await response.aread()))

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
                event_hooks={"response": [capture_page_response]},
            ) as client:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp",
                    http_client=client,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "chatgpt_export_page",
                            {
                                "session_id": "session-a",
                                "export_id": "mcp-page",
                                "max_response_bytes": MAX_PAGE_RESPONSE_BYTES,
                            },
                        )
                        assert result.isError is False
                        assert result.content == []
                        page = result.structuredContent
                        assert isinstance(page, dict)
                        tool_result_bytes = len(
                            json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        )
                        assert tool_result_bytes <= MAX_PAGE_RESPONSE_BYTES
                        assert isinstance(page["message_segment"], dict)
                        assert len(wire_lengths) == 1
                        assert wire_lengths[0] <= 16 * 1024

    asyncio.run(run())
