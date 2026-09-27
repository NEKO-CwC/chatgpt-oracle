from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from typing import Any
from urllib.parse import parse_qs, quote

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult

from .config import Settings
from .browser import BrowserNeedsUserAction, BrowserTaskError
from .jobs import ExportRequestError, JobManager
from .models import public_job
from .scheduler import IdempotencyConflict, SchedulerBusy
from .exports import (
    AttachmentUnavailable,
    ExportCapacityError,
    ExportSessionMismatch,
    InvalidExportCursor,
    MAX_PAGE_RESPONSE_BYTES,
    PageLimitTooSmall,
)
from .uploads import UploadError, UploadStore
from .file_store import FileTransferError

logging.basicConfig(stream=sys.stderr, level=os.getenv("CHATGPT_ORACLE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("chatgpt_oracle")
_TUS_VERSION = "1.0.0"
_TUS_MIME_TYPE = "application/offset+octet-stream"


def _single_byte_range(value: str, total_size: int) -> tuple[int, int]:
    if "," in value:
        raise FileTransferError("RANGE_NOT_SATISFIABLE", "multiple byte ranges are not supported", 416)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise FileTransferError("RANGE_NOT_SATISFIABLE", "invalid byte range", 416)
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0 or total_size == 0:
            raise FileTransferError("RANGE_NOT_SATISFIABLE", "byte range is outside the file", 416)
        return max(0, total_size - suffix), total_size - 1
    start = int(first)
    end = int(last) if last else total_size - 1
    if start >= total_size or end < start or total_size == 0:
        raise FileTransferError("RANGE_NOT_SATISFIABLE", "byte range is outside the file", 416)
    return start, min(end, total_size - 1)


class _PersistentLogFilter(logging.Filter):
    _url = re.compile(r"https?://[^\s\"']+", re.I)
    _bearer = re.compile(r"\bBearer\s+[^\s,;]+", re.I)
    _credential = re.compile(r"\b(cookie|authorization|password|token|secret)\s*[:=]\s*[^\s,;]+", re.I)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = self._bearer.sub("Bearer [REDACTED]", message)
        message = self._credential.sub(r"\1=[REDACTED]", message)
        message = self._url.sub("[URL]", message)
        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            message = f"{message} exception_type={exc_type}"
        record.msg = message
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def _configure_persistent_logging() -> None:
    log_dir = settings.root / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / f"chatgpt-oracle-{os.getpid()}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    scrubber = _PersistentLogFilter()
    for current in root_logger.handlers:
        current.addFilter(scrubber)
    handler.addFilter(scrubber)
    root_logger.addHandler(handler)


settings = Settings.from_env()
settings.validate_http_security()
manager = JobManager(settings, export_owner=settings.transport != "stdio")
upload_store = manager.uploads
mcp = FastMCP(
    "chatgpt-oracle",
    instructions=(
        "Delegate external research and second opinions to the user's logged-in ChatGPT Web profile. "
        "Use semantic task tools; never expose cookies or browser control primitives."
    ),
    host=settings.host,
    port=settings.port,
    streamable_http_path=settings.mcp_path,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
    ),
)


def _export_proxy_sync(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.bearer_token:
        return {"accepted": False, "error_code": "BROKER_UNAVAILABLE", "error": "HTTP Broker token is not configured"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{settings.port}/internal/exports/{action}",
        data=body,
        headers={
            "Authorization": f"Bearer {settings.bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {"accepted": False, "error_code": "BROKER_UNAVAILABLE", "error": "HTTP Broker is unavailable"}
    return value if isinstance(value, dict) else {"accepted": False, "error_code": "BROKER_ERROR"}


def _file_http_origin() -> str | None:
    if settings.public_base_url:
        return settings.public_base_url
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    host = f"[{settings.host}]" if ":" in settings.host else settings.host
    return f"http://{host}:{settings.port}"


def _file_content_url(file_ref: str) -> str:
    path = f"/v2/files/{quote(file_ref, safe='')}/content"
    origin = _file_http_origin()
    return f"{origin}{path}" if origin else path


def _file_refs_from_request(files: list[dict[str, str]] | None) -> list[str] | None:
    if files is None:
        return None
    references: list[str] = []
    for entry in files:
        if set(entry) != {"file_ref"} or not entry["file_ref"]:
            raise ValueError("each files entry must contain only a nonempty file_ref")
        references.append(entry["file_ref"])
    if len(references) != len(set(references)):
        raise ValueError("files contains a duplicate file_ref")
    return references


async def _export_proxy(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_export_proxy_sync, action, payload)


async def _manager_export_action(
    action: str, payload: dict[str, Any], *, owner: JobManager | None = None
) -> dict[str, Any]:
    action_manager = owner or manager
    await action_manager.start()
    if action == "start":
        return await manager.export_start(**payload)
    if action == "status":
        return await manager.export_status(str(payload["session_id"]), str(payload["export_id"]))
    if action == "page":
        return await manager.export_page(**payload)
    if action == "cancel":
        return await manager.export_cancel(str(payload["session_id"]), str(payload["export_id"]))
    if action == "attachment-handle":
        return await manager.export_attachment_handle(**payload)
    if action == "release":
        return await manager.export_release(str(payload["session_id"]), str(payload["export_id"]))
    if action == "legacy":
        return await manager.export(**payload)
    if action == "session-status":
        return await manager.session_status(str(payload["session_id"]))
    if action == "session-close":
        return await manager.session_close(str(payload["session_id"]))
    if action == "file-upload-start":
        session_id = str(payload["session_id"])
        if action_manager.sessions.get(session_id) is None:
            raise FileTransferError("SESSION_NOT_FOUND", "named session not found", 404)
        filename = str(payload["filename"])
        size = int(payload["size_bytes"])
        if size < 0 or size > settings.file_store_max_file_bytes:
            raise FileTransferError("FILE_TOO_LARGE", "file size is outside the supported range", 413)
        mime_type = str(payload.get("mime_type") or "application/octet-stream")
        metadata = {
            "session_id": session_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": str(size),
        }
        for name in ("sha256", "request_id"):
            if payload.get(name) is not None:
                metadata[name] = str(payload[name])
        if metadata.get("request_id") is not None and (
            metadata.get("sha256") is None or not re.fullmatch(r"[0-9a-fA-F]{64}", metadata["sha256"])
        ):
            raise FileTransferError(
                "IDEMPOTENCY_REQUIRES_DIGEST", "request_id requires a valid SHA-256 digest"
            )
        endpoint_path = "/v2/uploads"
        origin = _file_http_origin()
        return {
            "session_id": session_id,
            "endpoint": f"{origin}{endpoint_path}" if origin else endpoint_path,
            "endpoint_path": endpoint_path,
            "protocol": "tus",
            "tus_version": "1.0.0",
            "extensions": ["creation", "termination"],
            "headers": {"X-Oracle-Session-ID": session_id},
            "metadata": metadata,
            "max_file_bytes": settings.file_store_max_file_bytes,
            "max_chunk_bytes": settings.file_store_max_chunk_bytes,
            "expires_in_seconds": settings.file_store_ttl_seconds,
        }
    if action == "file-download-start":
        return await action_manager.start_attachment_download(**payload)
    if action == "file-transfer-status":
        session_id = str(payload["session_id"])
        result = action_manager.files.transfer_status(str(payload["transfer_id"]), session_id)
        if result["state"] == "READY":
            file_ref = str(result["file_ref"])
            result["file"] = action_manager.files.get_file(session_id, file_ref).public(
                content_url=_file_content_url(file_ref)
            )
        return result
    if action == "file-transfer-cancel":
        return await action_manager.cancel_file_download(str(payload["session_id"]), str(payload["transfer_id"]))
    if action == "file-info":
        session_id = str(payload["session_id"])
        file_ref = str(payload["file_ref"])
        return action_manager.files.get_file(session_id, file_ref).public(
            content_url=_file_content_url(file_ref)
        )
    if action == "file-release":
        return action_manager.files.release_file(str(payload["session_id"]), str(payload["file_ref"]))
    raise ValueError("unknown export operation")


async def _export_call(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if settings.transport == "stdio":
        return await _export_proxy(action, payload)
    try:
        return await _manager_export_action(action, payload)
    except BrowserNeedsUserAction as exc:
        return {
            "accepted": False,
            "error_code": "NEEDS_USER_INPUT",
            "error": str(exc),
            "instructions": exc.instructions,
        }
    except SchedulerBusy as exc:
        return exc.info.as_dict()
    except IdempotencyConflict:
        return {"accepted": False, "error_code": "IDEMPOTENCY_CONFLICT", "error": "request_id was already used with different inputs"}
    except ExportSessionMismatch:
        return {"accepted": False, "error_code": "EXPORT_SESSION_MISMATCH", "error": "export does not belong to this session"}
    except InvalidExportCursor:
        return {"accepted": False, "error_code": "CURSOR_INVALID", "error": "cursor is invalid or belongs to another view"}
    except PageLimitTooSmall:
        return {"accepted": False, "error_code": "PAGE_LIMIT_TOO_SMALL", "error": "max_response_bytes is outside the supported range"}
    except AttachmentUnavailable:
        return {"accepted": False, "error_code": "ATTACHMENT_UNAVAILABLE", "error": "attachment is not available for download"}
    except ExportCapacityError:
        return {"accepted": False, "error_code": "EXPORT_CAPACITY", "error": "snapshot storage limit reached"}
    except ExportRequestError as exc:
        return {"accepted": False, "error_code": exc.code, "error": str(exc)}
    except KeyError:
        return {"accepted": False, "error_code": "EXPORT_NOT_FOUND", "error": "export or session was not found"}
    except ValueError as exc:
        return {"accepted": False, "error_code": "EXPORT_ERROR", "error": str(exc)}


@mcp.tool()
async def chatgpt_submit(
    mode: str,
    prompt: str,
    project: str | None = None,
    model: str | None = None,
    quality: str = "auto",
    reasoning_effort: str | None = None,
    allow_downgrade: bool = False,
    on_busy: str = "reject",
    request_id: str | None = None,
    upload_ids: list[str] | None = None,
    files: list[dict[str, str]] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Submit a task with dynamic ChatGPT model/ability selection."""
    try:
        job = await manager.submit(
            mode,
            prompt,
            project=project,
            model=model,
            quality=quality,
            reasoning_effort=reasoning_effort,
            allow_downgrade=allow_downgrade,
            on_busy=on_busy,
            request_id=request_id,
            upload_ids=upload_ids,
            file_refs=_file_refs_from_request(files),
            session_id=session_id,
        )
        return manager.public(job.id)
    except SchedulerBusy as exc:
        return exc.info.as_dict()
    except UploadError as exc:
        return {"accepted": False, "error_code": "UPLOAD_ERROR", "error": str(exc)}
    except FileTransferError as exc:
        return {"accepted": False, "error_code": exc.code, "error": str(exc)}
    except ValueError as exc:
        return {"accepted": False, "error_code": "INVALID_REQUEST", "error": str(exc)}


@mcp.tool()
async def chatgpt_status(task_id: str) -> dict[str, Any]:
    """Return the durable state of a task without blocking for completion."""
    await manager.start()
    return manager.public(task_id)


@mcp.tool()
async def chatgpt_result(task_id: str, delivery: str = "legacy") -> dict[str, Any]:
    """Return the completed Markdown result and source links, or readiness information."""
    await manager.start()
    if delivery not in {"legacy", "inline", "reference"}:
        return {"accepted": False, "error_code": "INVALID_DELIVERY", "error": "delivery must be legacy, inline, or reference"}
    if delivery == "reference":
        return {**manager.public(task_id), **manager.result_reference(task_id), "delivery": "reference"}
    job, result = manager.result(task_id)
    response = manager.public(task_id)
    response["ready"] = job.state == "COMPLETE" and result is not None
    if delivery == "inline" and result is not None:
        result_bytes = result.encode("utf-8")
        if len(result_bytes) > settings.export_inline_max_bytes:
            page = manager.result_reference(task_id)
            return {
                **response,
                "ready": True,
                "result_markdown": None,
                "error_code": "RESULT_TOO_LARGE",
                "result_id": page["result_id"],
                "total_bytes": page["total_bytes"],
                "sha256": page["sha256"],
                "read_tool": "chatgpt_result_page",
            }
    response["result_markdown"] = result if response["ready"] else None
    return response


@mcp.tool()
async def chatgpt_result_page(
    session_id: str,
    result_id: str,
    cursor: str | None = None,
    max_response_bytes: int = 8192,
) -> CallToolResult:
    """Read a bounded UTF-8 page from an immutable task result."""
    await manager.start()
    try:
        result = manager.result_page(
            session_id,
            result_id,
            cursor=cursor,
            max_response_bytes=max_response_bytes,
        )
        return CallToolResult(content=[], structuredContent=result)
    except FileTransferError as exc:
        return CallToolResult(
            content=[],
            structuredContent={"accepted": False, "error_code": exc.code, "error": str(exc)},
        )
    except KeyError:
        return CallToolResult(
            content=[],
            structuredContent={"accepted": False, "error_code": "RESULT_NOT_FOUND"},
        )


@mcp.tool()
async def chatgpt_export(
    task_id: str | None = None,
    conversation_url: str | None = None,
    session_id: str | None = None,
    scope: str = "recent",
    count: int = 10,
    request_id: str | None = None,
    on_busy: str = "reject",
) -> dict[str, Any]:
    """Legacy inline export; use the paged export tools for long conversations."""
    return await _export_call(
        "legacy",
        {
            "task_id": task_id,
            "conversation_url": conversation_url,
            "session_id": session_id,
            "scope": scope,
            "count": count,
            "request_id": request_id,
            "on_busy": on_busy,
        },
    )


@mcp.tool()
async def chatgpt_export_start(
    session_id: str | None = None,
    task_id: str | None = None,
    conversation_url: str | None = None,
    scope: str = "all",
    count: int = 10,
    request_id: str | None = None,
    on_busy: str = "reject",
) -> dict[str, Any]:
    """Start a session-bound immutable export snapshot and return its export_id."""
    return await _export_call(
        "start",
        {
            "session_id": session_id,
            "task_id": task_id,
            "conversation_url": conversation_url,
            "scope": scope,
            "count": count,
            "request_id": request_id,
            "on_busy": on_busy,
        },
    )


@mcp.tool()
async def chatgpt_export_status(session_id: str, export_id: str) -> dict[str, Any]:
    """Return lifecycle state, completeness evidence, view sizes, and hashes."""
    return await _export_call("status", {"session_id": session_id, "export_id": export_id})


@mcp.tool()
async def chatgpt_export_cancel(session_id: str, export_id: str) -> dict[str, Any]:
    """Cancel a queued or running export and wait briefly for its browser slot to be released."""
    return await _export_call("cancel", {"session_id": session_id, "export_id": export_id})


@mcp.tool()
async def chatgpt_export_page(
    session_id: str,
    export_id: str,
    view: str = "messages",
    cursor: str | None = None,
    max_response_bytes: int = MAX_PAGE_RESPONSE_BYTES,
) -> CallToolResult:
    """Read structured message objects or a bounded compatibility/archive view."""
    result = await _export_call(
        "page",
        {
            "session_id": session_id,
            "export_id": export_id,
            "view": view,
            "cursor": cursor,
            "max_response_bytes": max_response_bytes,
        },
    )
    return CallToolResult(content=[], structuredContent=result)


@mcp.tool()
async def chatgpt_export_attachment_handle(
    session_id: str,
    export_id: str,
    attachment_id: str,
) -> dict[str, Any]:
    """Create a short-lived download handle for an explicitly requested attachment."""
    return await _export_call(
        "attachment-handle",
        {"session_id": session_id, "export_id": export_id, "attachment_id": attachment_id},
    )


@mcp.tool()
async def chatgpt_export_release(session_id: str, export_id: str) -> dict[str, Any]:
    """Release a READY snapshot. Use chatgpt_export_cancel for queued or running work."""
    return await _export_call("release", {"session_id": session_id, "export_id": export_id})


@mcp.tool()
async def chatgpt_file_upload_start(
    session_id: str,
    filename: str,
    size_bytes: int,
    mime_type: str | None = None,
    sha256: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Return the tus Creation endpoint and metadata for a session-scoped file upload."""
    return await _export_call(
        "file-upload-start",
        {
            "session_id": session_id,
            "filename": filename,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "sha256": sha256,
            "request_id": request_id,
        },
    )


@mcp.tool()
async def chatgpt_file_download_start(
    session_id: str,
    export_id: str,
    attachment_id: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Prepare a verified attachment into the shared streaming file store."""
    return await _export_call(
        "file-download-start",
        {
            "session_id": session_id,
            "export_id": export_id,
            "attachment_id": attachment_id,
            "request_id": request_id,
        },
    )


@mcp.tool()
async def chatgpt_transfer_status(session_id: str, transfer_id: str) -> dict[str, Any]:
    """Read a tus upload's durable offset and its file_ref after byte verification."""
    return await _export_call(
        "file-transfer-status", {"session_id": session_id, "transfer_id": transfer_id}
    )


@mcp.tool()
async def chatgpt_transfer_cancel(session_id: str, transfer_id: str) -> dict[str, Any]:
    """Cancel an unfinished file upload without releasing an already completed file."""
    return await _export_call(
        "file-transfer-cancel", {"session_id": session_id, "transfer_id": transfer_id}
    )


@mcp.tool()
async def chatgpt_file_info(session_id: str, file_ref: str) -> dict[str, Any]:
    """Return verified metadata and the authenticated content route for a READY file_ref."""
    return await _export_call("file-info", {"session_id": session_id, "file_ref": file_ref})


@mcp.tool()
async def chatgpt_file_release(session_id: str, file_ref: str) -> dict[str, Any]:
    """Release one session-scoped file_ref after its active readers finish."""
    return await _export_call("file-release", {"session_id": session_id, "file_ref": file_ref})


@mcp.tool()
async def chatgpt_resume(
    session_id: str,
    prompt: str,
    mode: str | None = None,
    quality: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    allow_downgrade: bool | None = None,
    on_busy: str = "reject",
    request_id: str | None = None,
    upload_ids: list[str] | None = None,
    files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Resume a named local session and append a message to its conversation."""
    try:
        job = await manager.resume_session(
            session_id,
            prompt,
            mode=mode,
            quality=quality,
            model=model,
            reasoning_effort=reasoning_effort,
            allow_downgrade=allow_downgrade,
            on_busy=on_busy,
            request_id=request_id,
            upload_ids=upload_ids,
            file_refs=_file_refs_from_request(files),
        )
        return manager.public(job.id)
    except SchedulerBusy as exc:
        return exc.info.as_dict()
    except UploadError as exc:
        return {"accepted": False, "error_code": "UPLOAD_ERROR", "error": str(exc)}
    except FileTransferError as exc:
        return {"accepted": False, "error_code": exc.code, "error": str(exc)}
    except (KeyError, ValueError) as exc:
        return {"accepted": False, "error_code": "SESSION_ERROR", "error": str(exc)}


@mcp.tool()
async def chatgpt_session_status(session_id: str) -> dict[str, Any]:
    """Return a named session's URL, liveness deadline and tab presence."""
    try:
        await manager.start()
        if settings.transport == "stdio":
            return await _export_proxy("session-status", {"session_id": session_id})
        return await manager.session_status(session_id)
    except (KeyError, ValueError) as exc:
        return {"accepted": False, "error_code": "SESSION_ERROR", "error": str(exc)}


@mcp.tool()
async def chatgpt_session_create(session_id: str | None = None) -> dict[str, Any]:
    """Create or return a durable named session before the first submit."""
    try:
        await manager.start()
        return manager.sessions.create(session_id).public(tab_open=False)
    except ValueError as exc:
        return {"accepted": False, "error_code": "SESSION_ERROR", "error": str(exc)}


@mcp.tool()
async def chatgpt_session_close(session_id: str) -> dict[str, Any]:
    """Mark a session dead and close only its ChatGPT tab."""
    try:
        await manager.start()
        if settings.transport == "stdio":
            return await _export_proxy("session-close", {"session_id": session_id})
        return await manager.session_close(session_id)
    except SchedulerBusy as exc:
        return exc.info.as_dict()
    except (KeyError, ValueError) as exc:
        return {"accepted": False, "error_code": "SESSION_ERROR", "error": str(exc)}


@mcp.tool()
async def chatgpt_sessions(include_dead: bool = True) -> dict[str, Any]:
    """List local session IDs without exposing browser credentials or content."""
    await manager.start()
    return {"sessions": await manager.session_list(include_dead=include_dead)}


@mcp.tool()
async def chatgpt_continue(
    task_id: str,
    prompt: str,
    quality: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    allow_downgrade: bool | None = None,
    on_busy: str = "reject",
    request_id: str | None = None,
    upload_ids: list[str] | None = None,
    files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Continue a conversation, inheriting or overriding model/ability selection."""
    try:
        job = await manager.continue_task(
            task_id,
            prompt,
            quality=quality,
            model=model,
            reasoning_effort=reasoning_effort,
            allow_downgrade=allow_downgrade,
            on_busy=on_busy,
            request_id=request_id,
            upload_ids=upload_ids,
            file_refs=_file_refs_from_request(files),
        )
        return manager.public(job.id)
    except SchedulerBusy as exc:
        return exc.info.as_dict()
    except UploadError as exc:
        return {"accepted": False, "error_code": "UPLOAD_ERROR", "error": str(exc)}
    except FileTransferError as exc:
        return {"accepted": False, "error_code": exc.code, "error": str(exc)}


@mcp.tool()
async def chatgpt_cancel(task_id: str) -> dict[str, Any]:
    """Cancel a queued or running task."""
    await manager.start()
    job = await manager.cancel(task_id)
    return public_job(job)


@mcp.tool()
async def chatgpt_takeover(task_id: str) -> dict[str, Any]:
    """Explain the manual action required for a blocked task; never automates authentication or CAPTCHA."""
    await manager.start()
    job = manager.get(task_id)
    return {
        **public_job(job),
        "takeover_required": job.state == "NEEDS_USER_INPUT",
        "instructions": job.user_action
        or "没有检测到需要人工接管的页面状态；先查询任务状态。",
        "profile_dir": str(settings.browser_profile_dir),
    }


@mcp.tool()
async def chatgpt_browser_status() -> dict[str, Any]:
    """Start or inspect the dedicated browser profile and report login readiness."""
    await manager.start()
    return await manager.browser.browser_status()


@mcp.tool()
async def chatgpt_capabilities(scope: str = "browser") -> dict[str, Any]:
    """Discover service file-transfer limits or visible ChatGPT model options."""
    if scope == "service":
        return {
            "api_revision": 3,
            "file_storage": "temporary",
            "public_base_url_configured": settings.public_base_url is not None,
            "upload": {
                "protocol": "tus",
                "version": "1.0.0",
                "extensions": ["creation", "termination"],
                "creation_with_upload": False,
                "max_file_bytes": settings.file_store_max_file_bytes,
                "max_chunk_bytes": settings.file_store_max_chunk_bytes,
                "max_transfers": settings.file_store_max_transfers,
                "ttl_seconds": settings.file_store_ttl_seconds,
            },
            "download": {
                "range": "single-byte-range",
                "repeatable": True,
                "storage_ttl_seconds": settings.file_store_ttl_seconds,
            },
            "result_pages": {"max_response_bytes": 12 * 1024, "enabled": True},
            "source_attachment_materialization": {"enabled": True},
        }
    if scope != "browser":
        return {"accepted": False, "error_code": "INVALID_SCOPE", "error": "scope must be browser or service"}
    await manager.start()
    return asdict(await manager.browser.capabilities())


@mcp.tool()
async def chatgpt_health() -> dict[str, Any]:
    """Return service configuration and recoverable task counts without exposing secrets."""
    await manager.start()
    jobs = manager.store.all()
    return {
        "service": "chatgpt-oracle",
        "version": "0.1.0",
        "transport": settings.transport,
        "host": settings.host,
        "port": settings.port,
        "mcp_path": settings.mcp_path,
        "profile_dir": str(settings.browser_profile_dir),
        "jobs": {
            "total": len(jobs),
            "active": sum(
                job.state in {"QUEUED", "STARTING", "UPLOADING", "ATTACHMENTS_VERIFIED", "SEND_PENDING", "RUNNING"}
                for job in jobs
            ),
            "blocked": sum(job.state == "NEEDS_USER_INPUT" for job in jobs),
        },
        "scheduler": manager.scheduler.status(),
    }


class AuthHealthMiddleware:
    """Minimal bearer-token gate around the MCP ASGI app."""

    def __init__(self, app: Any, token: str | None, uploads: UploadStore, manager: JobManager | None = None):
        self.app = app
        self.token = token
        self.uploads = uploads
        self.manager = manager

    @staticmethod
    async def _send_json(send: Any, status: int, value: dict[str, Any], *, no_store: bool = False) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(payload)).encode()),
        ]
        if no_store:
            headers.append((b"cache-control", b"no-store"))
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    async def _read_json_body(receive: Any, *, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                raise ValueError("request disconnected")
            if message.get("type") != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > max_bytes:
                raise ValueError("request body is too large")
            if not message.get("more_body", False):
                break
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    async def _dispatch_export(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.manager is None:
            return {"accepted": False, "error_code": "BROKER_UNAVAILABLE", "error": "export manager is unavailable"}
        try:
            await self.manager.start()
            if action == "start":
                return await self.manager.export_start(**payload)
            if action == "status":
                return await self.manager.export_status(str(payload["session_id"]), str(payload["export_id"]))
            if action == "page":
                return await self.manager.export_page(**payload)
            if action == "cancel":
                return await self.manager.export_cancel(str(payload["session_id"]), str(payload["export_id"]))
            if action == "attachment-handle":
                return await self.manager.export_attachment_handle(**payload)
            if action == "release":
                return await self.manager.export_release(str(payload["session_id"]), str(payload["export_id"]))
            if action == "legacy":
                return await self.manager.export(**payload)
            if action == "session-status":
                return await self.manager.session_status(str(payload["session_id"]))
            if action == "session-close":
                return await self.manager.session_close(str(payload["session_id"]))
            if action == "file-upload-start":
                return await _manager_export_action(action, payload, owner=self.manager)
            if action == "file-download-start":
                return await self.manager.start_attachment_download(**payload)
            if action == "file-transfer-status":
                return await _manager_export_action(action, payload, owner=self.manager)
            if action == "file-transfer-cancel":
                return await _manager_export_action(action, payload, owner=self.manager)
            if action == "file-info":
                return await _manager_export_action(action, payload, owner=self.manager)
            if action == "file-release":
                return await _manager_export_action(action, payload, owner=self.manager)
            return {"accepted": False, "error_code": "EXPORT_ERROR", "error": "unknown export operation"}
        except BrowserNeedsUserAction as exc:
            return {
                "accepted": False,
                "error_code": "NEEDS_USER_INPUT",
                "error": str(exc),
                "instructions": exc.instructions,
            }
        except SchedulerBusy as exc:
            return exc.info.as_dict()
        except IdempotencyConflict:
            return {"accepted": False, "error_code": "IDEMPOTENCY_CONFLICT", "error": "request_id was already used with different inputs"}
        except ExportSessionMismatch:
            return {"accepted": False, "error_code": "EXPORT_SESSION_MISMATCH", "error": "export does not belong to this session"}
        except InvalidExportCursor:
            return {"accepted": False, "error_code": "CURSOR_INVALID", "error": "cursor is invalid or belongs to another view"}
        except PageLimitTooSmall:
            return {"accepted": False, "error_code": "PAGE_LIMIT_TOO_SMALL", "error": "max_response_bytes is outside the supported range"}
        except FileTransferError as exc:
            return {"accepted": False, "error_code": exc.code, "error": str(exc), "http_status": exc.status_code}
        except AttachmentUnavailable:
            return {"accepted": False, "error_code": "ATTACHMENT_UNAVAILABLE", "error": "attachment is not available for download"}
        except ExportCapacityError:
            return {"accepted": False, "error_code": "EXPORT_CAPACITY", "error": "snapshot storage limit reached"}
        except ExportRequestError as exc:
            return {"accepted": False, "error_code": exc.code, "error": str(exc)}
        except KeyError:
            code = "SESSION_ERROR" if action in {"session-status", "session-close"} else "EXPORT_NOT_FOUND"
            return {"accepted": False, "error_code": code, "error": "export or session was not found"}
        except ValueError as exc:
            return {"accepted": False, "error_code": "EXPORT_ERROR", "error": str(exc)}

    @staticmethod
    async def _send_tus(
        send: Any,
        status: int,
        headers: list[tuple[bytes, bytes]] | None = None,
        body: bytes = b"",
        *,
        include_resumable: bool = True,
    ) -> None:
        response_headers = list(headers or [])
        if include_resumable:
            response_headers.append((b"tus-resumable", _TUS_VERSION.encode("ascii")))
        response_headers.append((b"content-length", str(len(body)).encode("ascii")))
        response_headers.append((b"cache-control", b"no-store"))
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _read_tus_chunk(receive: Any, *, max_bytes: int, content_length: int | None) -> bytes:
        if content_length is not None and content_length > max_bytes:
            raise FileTransferError("CHUNK_TOO_LARGE", "chunk exceeds the supported size", 413)
        data = bytearray()
        while True:
            event = await receive()
            if event.get("type") == "http.disconnect":
                raise FileTransferError("UPLOAD_DISCONNECTED", "client disconnected during upload", 400)
            if event.get("type") != "http.request":
                continue
            chunk = event.get("body", b"")
            if len(data) + len(chunk) > max_bytes:
                raise FileTransferError("CHUNK_TOO_LARGE", "chunk exceeds the supported size", 413)
            data.extend(chunk)
            if not event.get("more_body", False):
                break
        if content_length is not None and len(data) != content_length:
            raise FileTransferError("CONTENT_LENGTH_MISMATCH", "request length does not match Content-Length", 400)
        return bytes(data)

    @staticmethod
    def _decode_tus_metadata(value: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if not value.strip():
            return metadata
        for entry in value.split(","):
            key, separator, encoded = entry.strip().partition(" ")
            if not key or key in metadata:
                raise FileTransferError("INVALID_UPLOAD_METADATA", "Upload-Metadata contains a duplicate or empty key")
            if separator:
                try:
                    metadata[key] = base64.b64decode(encoded, validate=True).decode("utf-8")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise FileTransferError("INVALID_UPLOAD_METADATA", "Upload-Metadata is not valid base64 UTF-8") from exc
            else:
                metadata[key] = ""
        return metadata

    async def _handle_v2_files(self, scope: dict[str, Any], receive: Any, send: Any) -> bool:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "")).upper()
        headers = {key.decode("ascii").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}

        if self.manager is None:
            if path.startswith("/v2/"):
                await self._send_json(send, 503, {"error_code": "BROKER_UNAVAILABLE"}, no_store=True)
                return True
            return False

        if path == "/v2/uploads" and method == "OPTIONS":
            await self._send_tus(
                send,
                204,
                [
                    (b"tus-version", _TUS_VERSION.encode("ascii")),
                    (b"tus-extension", b"creation,termination"),
                    (b"tus-max-size", str(self.manager.settings.file_store_max_file_bytes).encode("ascii")),
                    (b"accept-patch", _TUS_MIME_TYPE.encode("ascii")),
                ],
                include_resumable=False,
            )
            return True

        upload_parts = [part for part in path.split("/") if part]
        if len(upload_parts) == 2 and upload_parts == ["v2", "uploads"] and method == "POST":
            if self.manager is None:
                await self._send_tus(send, 503, body=b"Broker unavailable")
                return True
            try:
                await self.manager.start()
                if headers.get("tus-resumable") != _TUS_VERSION:
                    raise FileTransferError("TUS_VERSION_MISMATCH", "Tus-Resumable must be 1.0.0", 412)
                try:
                    upload_length = int(headers["upload-length"])
                    content_length = int(headers.get("content-length", "0"))
                except (KeyError, ValueError) as exc:
                    raise FileTransferError("INVALID_UPLOAD_LENGTH", "valid Upload-Length is required") from exc
                if content_length != 0:
                    raise FileTransferError("CREATION_WITH_UPLOAD_UNSUPPORTED", "send bytes with tus PATCH")
                await self._read_tus_chunk(receive, max_bytes=0, content_length=0)
                metadata = self._decode_tus_metadata(headers.get("upload-metadata", ""))
                session_header = headers.get("x-oracle-session-id")
                if not session_header or session_header != metadata.get("session_id"):
                    raise FileTransferError("SESSION_MISMATCH", "session metadata and X-Oracle-Session-ID must match", 403)
                if self.manager.sessions.get(session_header) is None:
                    raise FileTransferError("SESSION_NOT_FOUND", "named session not found", 404)
                if metadata.get("filename") is None:
                    raise FileTransferError("INVALID_FILENAME", "Upload-Metadata filename is required")
                if metadata.get("size_bytes") is not None and int(metadata["size_bytes"]) != upload_length:
                    raise FileTransferError("UPLOAD_LENGTH_MISMATCH", "Upload-Length differs from metadata", 409)
                transfer = self.manager.files.create_upload(
                    session_id=session_header,
                    filename=metadata["filename"],
                    size=upload_length,
                    mime_type=metadata.get("mime_type"),
                    sha256=metadata.get("sha256"),
                    request_id=metadata.get("request_id"),
                )
                location = f"/v2/uploads/{transfer['transfer_id']}"
                await self._send_tus(
                    send,
                    201,
                    [
                        (b"location", location.encode("ascii")),
                        (b"upload-offset", str(transfer["committed_offset"]).encode("ascii")),
                        (b"upload-length", str(upload_length).encode("ascii")),
                    ],
                )
            except FileTransferError as exc:
                await self._send_tus(
                    send,
                    exc.status_code,
                    [(b"x-error-code", exc.code.encode("ascii", "replace"))],
                    str(exc).encode("utf-8"),
                )
            return True

        if len(upload_parts) == 3 and upload_parts[:2] == ["v2", "uploads"] and method in {"HEAD", "PATCH", "DELETE"}:
            if self.manager is None:
                await self._send_tus(send, 503, body=b"Broker unavailable")
                return True
            try:
                await self.manager.start()
                if headers.get("tus-resumable") != _TUS_VERSION:
                    raise FileTransferError("TUS_VERSION_MISMATCH", "Tus-Resumable must be 1.0.0", 412)
                query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
                session_id = headers.get("x-oracle-session-id") or (query.get("session_id") or [None])[0]
                if not session_id:
                    raise FileTransferError("SESSION_REQUIRED", "X-Oracle-Session-ID is required", 403)
                transfer_id = upload_parts[2]
                if method == "DELETE":
                    result = self.manager.files.cancel_transfer(session_id, transfer_id)
                    await self._send_tus(send, 204, [(b"x-transfer-state", str(result["state"]).encode("ascii"))])
                    return True
                status = self.manager.files.transfer_status(transfer_id, session_id)
                if status["state"] in {"CANCELLED", "EXPIRED", "FAILED"}:
                    raise FileTransferError("TRANSFER_GONE", "transfer is no longer available", 410)
                if method == "HEAD":
                    response_headers = [
                        (b"upload-offset", str(status["committed_offset"]).encode("ascii")),
                        (b"upload-length", str(status["total_bytes"]).encode("ascii")),
                        (b"cache-control", b"no-store"),
                    ]
                    if status["state"] == "READY":
                        response_headers.extend(
                            [
                                (b"x-oracle-file-ref", str(status["file_ref"]).encode("ascii")),
                                (b"x-content-sha256", str(status["sha256"]).encode("ascii")),
                            ]
                        )
                    await self._send_tus(send, 200, response_headers)
                    return True
                if status["state"] != "RECEIVING":
                    raise FileTransferError("TRANSFER_NOT_RECEIVING", "transfer is not accepting bytes", 409)
                if headers.get("content-type", "").split(";", 1)[0].strip().lower() != _TUS_MIME_TYPE:
                    raise FileTransferError("INVALID_PATCH_TYPE", f"Content-Type must be {_TUS_MIME_TYPE}", 415)
                try:
                    offset = int(headers["upload-offset"])
                    content_length = int(headers["content-length"]) if "content-length" in headers else None
                except (KeyError, ValueError) as exc:
                    raise FileTransferError("INVALID_PATCH_HEADERS", "valid Upload-Offset is required") from exc
                data = await self._read_tus_chunk(
                    receive,
                    max_bytes=self.manager.files.max_chunk_bytes,
                    content_length=content_length,
                )
                result = await asyncio.to_thread(
                    self.manager.files.append_chunk, transfer_id, session_id, offset, data
                )
                response_headers = [(b"upload-offset", str(result["committed_offset"]).encode("ascii"))]
                if result["state"] == "READY":
                    response_headers.extend(
                        [
                            (b"x-oracle-file-ref", str(result["file_ref"]).encode("ascii")),
                            (b"x-content-sha256", str(result["sha256"]).encode("ascii")),
                        ]
                    )
                await self._send_tus(send, 204, response_headers)
            except FileTransferError as exc:
                await self._send_tus(
                    send,
                    exc.status_code,
                    [(b"x-error-code", exc.code.encode("ascii", "replace"))],
                    str(exc).encode("utf-8"),
                )
            return True

        file_parts = [part for part in path.split("/") if part]
        if len(file_parts) == 4 and file_parts[:2] == ["v2", "files"] and file_parts[3] == "content" and method in {"GET", "HEAD"}:
            if self.manager is None:
                await self._send_json(send, 503, {"error_code": "BROKER_UNAVAILABLE"}, no_store=True)
                return True
            try:
                query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
                session_id = headers.get("x-oracle-session-id") or (query.get("session_id") or [None])[0]
                if not session_id:
                    raise FileTransferError("SESSION_REQUIRED", "X-Oracle-Session-ID is required", 403)
                file_ref = file_parts[2]
                record = self.manager.files.get_file(session_id, file_ref)
                etag = f'"{record.sha256}"'
                range_header = headers.get("range") if method == "GET" else None
                if range_header and headers.get("if-range", etag) != etag:
                    range_header = None
                start, end = (0, record.size - 1) if record.size else (0, -1)
                status_code = 200
                if range_header:
                    start, end = _single_byte_range(range_header, record.size)
                    status_code = 206
                length = end - start + 1 if end >= start else 0
                response_headers = [
                    (b"content-type", record.mime_type.encode("latin-1", "replace")),
                    (b"content-length", str(length).encode("ascii")),
                    (b"content-disposition", f"attachment; filename*=UTF-8''{quote(record.filename, safe='')}".encode("ascii")),
                    (b"etag", etag.encode("ascii")),
                    (b"accept-ranges", b"bytes"),
                    (b"x-content-sha256", record.sha256.encode("ascii")),
                    (b"cache-control", b"private, no-store, no-transform"),
                ]
                if status_code == 206:
                    response_headers.append(
                        (b"content-range", f"bytes {start}-{end}/{record.size}".encode("ascii"))
                    )
                if method == "HEAD":
                    await send({"type": "http.response.start", "status": 200, "headers": response_headers})
                    await send({"type": "http.response.body", "body": b""})
                    return True
                record, file_path = self.manager.files.read_path(session_id, file_ref)
                try:
                    with file_path.open("rb") as content:
                        content.seek(start)
                        await send({"type": "http.response.start", "status": status_code, "headers": response_headers})
                        remaining = length
                        while remaining:
                            block = content.read(min(self.manager.files.io_chunk_bytes, remaining))
                            if not block:
                                raise OSError("stored file ended before its recorded length")
                            remaining -= len(block)
                            await send(
                                {"type": "http.response.body", "body": block, "more_body": remaining > 0}
                            )
                        if not length:
                            await send({"type": "http.response.body", "body": b""})
                finally:
                    self.manager.files.finish_read(file_ref)
            except FileTransferError as exc:
                response_headers = [(b"content-range", f"bytes */{record.size}".encode("ascii"))] if exc.status_code == 416 else []
                response_headers.append((b"x-error-code", exc.code.encode("ascii", "replace")))
                await self._send_tus(send, exc.status_code, response_headers, str(exc).encode("utf-8"), include_resumable=False)
            except OSError as exc:
                logger.warning("file response failed error_type=%s", type(exc).__name__)
                await self._send_json(send, 500, {"error_code": "FILE_READ_ERROR"}, no_store=True)
            return True
        return False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in {"/healthz", "/readyz"}:
            payload = json.dumps({"ok": True, "service": "chatgpt-oracle"}).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
            })
            await send({"type": "http.response.body", "body": payload})
            return
        if self.token:
            headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
            expected = f"Bearer {self.token}"
            if headers.get("authorization") != expected:
                await self._send_json(send, 401, {"error": "unauthorized"})
                return
        if await self._handle_v2_files(scope, receive, send):
            return
        if scope.get("method") == "POST":
            parts = [part for part in path.split("/") if part]
            action: str | None = None
            path_export_id: str | None = None
            if len(parts) == 3 and parts[:2] == ["internal", "exports"]:
                client = scope.get("client")
                try:
                    loopback = bool(client and ipaddress.ip_address(client[0]).is_loopback)
                except ValueError:
                    loopback = False
                if not loopback:
                    await self._send_json(send, 404, {"error_code": "NOT_FOUND", "error": "not found"})
                    return
                action = parts[2]
            elif parts == ["exports", "start"]:
                action = "start"
            elif len(parts) == 3 and parts[0] == "exports" and parts[2] == "file":
                path_export_id = parts[1]
                action = "file"
            elif len(parts) == 3 and parts[0] == "exports" and parts[2] in {
                "status", "page", "release", "cancel", "attachment-handle"
            }:
                path_export_id = parts[1]
                action = parts[2]
            if action:
                try:
                    payload = await self._read_json_body(receive)
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                    await self._send_json(send, 400, {"accepted": False, "error_code": "INVALID_REQUEST", "error": "invalid JSON request body"}, no_store=True)
                    return
                if path_export_id:
                    payload["export_id"] = path_export_id
                if action == "file":
                    try:
                        if self.manager is None:
                            raise ExportRequestError("BROKER_UNAVAILABLE", "export manager is unavailable")
                        lease, content_type, filename = await self.manager.export_file(**payload)
                    except ExportSessionMismatch:
                        await self._send_json(send, 200, {"accepted": False, "error_code": "EXPORT_SESSION_MISMATCH"}, no_store=True)
                        return
                    except ExportRequestError as exc:
                        await self._send_json(send, 200, {"accepted": False, "error_code": exc.code}, no_store=True)
                        return
                    except (KeyError, ValueError):
                        await self._send_json(send, 200, {"accepted": False, "error_code": "EXPORT_ERROR"}, no_store=True)
                        return
                    sent_bytes = 0
                    stream_error: str | None = None
                    try:
                        headers = [
                            (b"content-type", content_type.encode("latin-1", "replace")),
                            (b"content-length", str(lease.size).encode("ascii")),
                            (b"x-content-sha256", lease.sha256.encode("ascii")),
                            (b"cache-control", b"no-store"),
                            (b"content-disposition", f'attachment; filename="{filename}"'.encode("ascii", "replace")),
                        ]
                        chunk_size = max(1, self.manager.settings.file_store_io_chunk_bytes)
                        with lease.path.open("rb") as stream:
                            await send({"type": "http.response.start", "status": 200, "headers": headers})
                            if lease.size == 0:
                                await send({"type": "http.response.body", "body": b""})
                            while sent_bytes < lease.size:
                                chunk = stream.read(min(chunk_size, lease.size - sent_bytes))
                                if not chunk:
                                    raise OSError("export snapshot view ended before its declared size")
                                sent_bytes += len(chunk)
                                await send({
                                    "type": "http.response.body",
                                    "body": chunk,
                                    "more_body": sent_bytes < lease.size,
                                })
                    except Exception as exc:
                        stream_error = type(exc).__name__
                        raise
                    finally:
                        self.manager.export_file_finished(
                            lease,
                            sent_bytes=sent_bytes,
                            error_type=stream_error,
                        )
                    return
                result = await self._dispatch_export(action, payload)
                await self._send_json(send, 200, result, no_store=True)
                return
        if self.manager and scope.get("method") == "GET":
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[0] == "exports":
                export_id = parts[1]
                payload = self.manager.get_export(export_id)
                if payload is None:
                    await self._send_json(send, 404, {"error_code": "EXPORT_NOT_FOUND", "error": "export not found or expired"}, no_store=True)
                else:
                    await self._send_json(send, 200, payload, no_store=True)
                return
            if len(parts) == 3 and parts[0] == "exports" and parts[1] == "download":
                handle_id = parts[2]
                try:
                    body, content_type, disposition = await self.manager.download_export_attachment(handle_id)
                except KeyError as exc:
                    await self._send_json(send, 404, {"error_code": "DOWNLOAD_NOT_FOUND", "error": str(exc)})
                    return
                except Exception as exc:
                    logger.warning("export attachment download failed error_type=%s", type(exc).__name__)
                    await self._send_json(send, 502, {"error_code": "DOWNLOAD_ERROR", "error": "attachment download failed"})
                    return
                headers = [
                    (b"content-type", (content_type or "application/octet-stream").encode("latin-1", "replace")),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"x-content-sha256", hashlib.sha256(body).hexdigest().encode("ascii")),
                    (b"cache-control", b"no-store"),
                ]
                if disposition:
                    headers.append((b"content-disposition", disposition.encode("latin-1", "replace")))
                await send({"type": "http.response.start", "status": 200, "headers": headers})
                if not body:
                    await send({"type": "http.response.body", "body": b""})
                else:
                    for offset in range(0, len(body), 64 * 1024):
                        chunk = body[offset : offset + 64 * 1024]
                        await send({
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": offset + len(chunk) < len(body),
                        })
                return
        if path == "/uploads" and scope.get("method") == "POST":
            headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
            query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
            filename = headers.get("x-upload-filename") or (query.get("filename") or [None])[0]
            if not filename:
                await self._send_json(send, 400, {"error_code": "UPLOAD_ERROR", "error": "filename is required"})
                return
            declared_length: int | None = None
            if headers.get("content-length"):
                try:
                    declared_length = int(headers["content-length"])
                except ValueError:
                    await self._send_json(send, 400, {"error_code": "UPLOAD_ERROR", "error": "invalid Content-Length"})
                    return
            try:
                metadata = await self.uploads.put_stream(
                    filename,
                    headers.get("content-type"),
                    lambda: receive(),
                    declared_length=declared_length,
                )
            except UploadError as exc:
                await self._send_json(send, exc.status_code, {"error_code": "UPLOAD_ERROR", "error": str(exc)})
                return
            await self._send_json(send, 201, metadata.public())
            return
        await self.app(scope, receive, send)


def run() -> None:
    _configure_persistent_logging()
    logger.info("service starting transport=%s port=%s", settings.transport, settings.port)
    transport = settings.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    import uvicorn

    app = AuthHealthMiddleware(mcp.streamable_http_app(), settings.bearer_token, upload_store, manager)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
