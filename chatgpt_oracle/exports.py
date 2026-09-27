"""Conversation export models and short-lived download handles.

The browser is the source of truth for conversation contents.  This module
only normalises the browser payload into a client-facing manifest and keeps
download handles in process memory; it never stores cookies or attachment
bytes on disk.
"""

from __future__ import annotations

import copy
import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Iterable as IterableABC
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlparse

MAX_PAGE_RESPONSE_BYTES = 15 * 1024


class ExportSessionMismatch(ValueError):
    pass


class InvalidExportCursor(ValueError):
    pass


class ExportCapacityError(RuntimeError):
    pass


class PageLimitTooSmall(ValueError):
    pass


class AttachmentUnavailable(ValueError):
    pass


_SECRET_KEY = re.compile(
    r"(?:cookie|token|authorization|session|password|secret|credential|storage_state|localstorage|otp)",
    re.I,
)


def _safe(value: Any) -> Any:
    """Drop credential-like mapping keys while retaining ordinary message text."""
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_safe(item) for item in value]
    return value


def _without_download_sources(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_download_sources(item)
            for key, item in value.items()
            if str(key).lower() not in {"source_url", "sourceurl", "download_url", "downloadurl", "signed_url"}
        }
    if isinstance(value, list):
        return [_without_download_sources(item) for item in value]
    if isinstance(value, tuple):
        return [_without_download_sources(item) for item in value]
    return value


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _is_browser_download_host(source_url: str) -> bool:
    parsed = urlparse(source_url)
    host = parsed.hostname or ""
    return host in {"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com", "oaiusercontent.com"} or host.endswith((".oaiusercontent.com", ".openai.com"))


def _normalise_messages(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        author_role = author.get("role") if isinstance(author, dict) else author
        role = str(item.get("role") or item.get("author_role") or author_role or "unknown").lower()
        if role not in {"user", "assistant", "system", "tool", "unknown"}:
            role = "unknown"
        message = dict(item)
        message["id"] = str(item.get("id") or item.get("message_id") or f"msg_{index + 1}")
        message["message_id"] = message["id"]
        message["role"] = role
        message["order"] = item.get("order", index)
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        body = item.get("content", item.get("text", item.get("body", "")))
        rich_parts = item.get("rich_parts") if isinstance(item.get("rich_parts"), list) else []
        if isinstance(body, dict):
            parts = body.get("parts")
            if isinstance(parts, list):
                text_parts: list[str] = []
                for part in parts:
                    if isinstance(part, (str, int, float)):
                        text_parts.append(str(part))
                    elif isinstance(part, dict):
                        candidate = part.get("text") or part.get("content") or part.get("value")
                        if isinstance(candidate, (str, int, float)):
                            text_parts.append(str(candidate))
                        else:
                            rich_parts.append(part)
                body = "\n".join(text_parts)
            else:
                body = body.get("text") or body.get("value") or ""
        if isinstance(body, list):
            parts: list[str] = []
            for part in body:
                if isinstance(part, dict):
                    candidate = part.get("text") or part.get("content") or part.get("value")
                    if isinstance(candidate, (str, int, float)):
                        parts.append(str(candidate))
                    else:
                        rich_parts.append(part)
                else:
                    parts.append(str(part))
            body = "\n".join(parts)
        message["text"] = str(body or "")
        if rich_parts:
            message["rich_parts"] = rich_parts
        message["channel"] = item.get("channel")
        message["message_type"] = metadata.get("message_type") or item.get("message_type")
        message["parent_id"] = metadata.get("parent_id") or item.get("parent_id")
        message["turn_exchange_id"] = metadata.get("turn_exchange_id") or item.get("turn_exchange_id")
        message["working_turn_id"] = metadata.get("working_turn_id") or item.get("working_turn_id")
        message["is_visually_hidden_from_conversation"] = bool(
            metadata.get("is_visually_hidden_from_conversation")
            or item.get("is_visually_hidden_from_conversation")
        )
        citations = item.get(
            "citations",
            item.get("references") or item.get("links") or metadata.get("citations") or metadata.get("content_references") or [],
        )
        code_blocks = item.get("code_blocks", item.get("code") or [])
        tables = item.get("tables") or []
        attachments = item.get("attachments") or metadata.get("attachments") or metadata.get("files") or []
        message["citations"] = citations if isinstance(citations, list) else ([citations] if citations else [])
        message["code_blocks"] = code_blocks if isinstance(code_blocks, list) else ([code_blocks] if code_blocks else [])
        message["tables"] = tables if isinstance(tables, list) else ([tables] if tables else [])
        message["attachments"] = attachments if isinstance(attachments, list) else ([attachments] if attachments else [])
        message["canvas"] = item.get("canvas") or metadata.get("canvas") or metadata.get("canvas_document")
        message["deep_research"] = item.get("deep_research") or metadata.get("deep_research") or metadata.get("content_references")
        message["metadata"] = metadata
        result.append(_safe(message))
    return result


def recent_turns(messages: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Return the last *count* user/assistant turns, preserving message order.

    A turn starts at a user message and includes following assistant/tool
    messages up to the next user message.  For pages that expose no user
    messages, the fallback is the last ``count`` messages.
    """
    if count <= 0 or not messages:
        return []
    if getattr(messages, "is_message_spool", False):
        recent_messages: deque[dict[str, Any]] = deque(maxlen=count)
        recent_turn_groups: deque[list[dict[str, Any]]] = deque(maxlen=count)
        has_user = False
        for message in messages:
            if message.get("role") == "user":
                has_user = True
                recent_turn_groups.append([message])
            elif has_user and recent_turn_groups:
                recent_turn_groups[-1].append(message)
            else:
                recent_messages.append(message)
        if has_user:
            return [message for group in recent_turn_groups for message in group]
        return list(recent_messages)
    starts = [index for index, message in enumerate(messages) if message.get("role") == "user"]
    if not starts:
        return messages[-count:]
    start = starts[max(0, len(starts) - count)]
    return messages[start:]


def _markdown(messages: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown").capitalize()
        text = str(message.get("text") or "").strip()
        message_id = str(message.get("id") or message.get("message_id") or "")
        section = (f"<!-- message_id: {message_id} -->\n" if message_id else "") + f"## {role}"
        if message.get("created_at") or message.get("timestamp"):
            section += f" ({message.get('created_at') or message.get('timestamp')})"
        sections.append(section + "\n\n" + text)
        for block in message.get("code_blocks") or []:
            if isinstance(block, dict):
                language = str(block.get("language") or "").strip()
                code = str(block.get("text") or block.get("code") or "")
            else:
                language, code = "", str(block)
            sections.append(f"```{language}\n{code}\n```")
        for table in message.get("tables") or []:
            if isinstance(table, dict):
                table_text = str(table.get("markdown") or table.get("text") or "")
            else:
                table_text = str(table)
            if table_text:
                sections.append(table_text)
        for field, title in (
            ("rich_parts", "Rich content"),
            ("canvas", "Canvas"),
            ("deep_research", "Deep Research"),
        ):
            value = message.get(field)
            if value:
                sections.append(
                    f"### {title}\n\n```json\n"
                    + json.dumps(_safe(_without_download_sources(value)), ensure_ascii=False, indent=2)
                    + "\n```"
                )
        attachments = message.get("attachments") or []
        if attachments:
            entries = []
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                filename = str(attachment.get("filename") or "attachment")
                details = [
                    str(attachment.get("mime_type")) if attachment.get("mime_type") else None,
                    f"{attachment.get('size')} bytes" if attachment.get("size") is not None else None,
                ]
                suffix = ", ".join(item for item in details if item)
                entries.append(f"- {filename}" + (f" ({suffix})" if suffix else ""))
            if entries:
                sections.append("### Attachments\n\n" + "\n".join(entries))
        for citation in message.get("citations") or []:
            if isinstance(citation, dict):
                label = citation.get("title") or citation.get("label") or citation.get("url")
                url = citation.get("url") or citation.get("href")
            else:
                label = url = citation
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                sections.append(f"[{label or url}]({url})")
    return "\n\n".join(sections).strip() + ("\n" if sections else "")


class _MarkdownStream:
    def __init__(self, messages: Any, attachments_by_message: dict[str, list[dict[str, Any]]]):
        self.messages = messages
        self.attachments_by_message = attachments_by_message

    def __iter__(self):
        first = True
        for message in self.messages:
            if not first:
                yield "\n\n"
            message_id = str(message.get("id") or message.get("message_id") or "")
            attachments = self.attachments_by_message.get(message_id)
            if attachments is not None:
                message = {**message, "attachments": attachments}
            yield _markdown([message]).rstrip("\n")
            first = False
        if not first:
            yield "\n"


@dataclass
class ExportEntry:
    export_id: str
    payload: dict[str, Any]
    expires_at: float
    response: dict[str, Any] | None = None


@dataclass
class ExportSnapshot:
    export_id: str
    session_id: str
    conversation_url: str
    source: str
    completeness: dict[str, str]
    reason: dict[str, str | None]
    storage_dir: Path
    view_paths: dict[str, Path]
    view_sizes: dict[str, int]
    view_hashes: dict[str, str]
    message_ranges: list[tuple[int, int]]
    message_ids: list[str | None]
    message_hashes: list[str]
    attachment_sources: dict[str, str]
    attachment_records: dict[str, dict[str, Any]]
    attachment_count: int
    created_at: float
    expires_at: float
    absolute_expires_at: float
    active_readers: int = 0
    retired: bool = False


@dataclass
class ExportViewLease:
    path: Path = dataclass_field(repr=False)
    view: str
    size: int
    sha256: str
    snapshot: ExportSnapshot = dataclass_field(repr=False)


@dataclass
class DownloadHandle:
    handle: str
    export_id: str
    attachment_id: str
    source_url: str
    expires_at: float
    session_id: str | None = None
    conversation_url: str | None = None
    consumed: bool = False

    def public_url(self, prefix: str = "/exports/download") -> str:
        return f"{prefix.rstrip('/')}/{self.handle}"


class ExportStore:
    """Process-local retention for export manifests and one-time attachment handles."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 900.0,
        max_lifetime_seconds: float = 7200.0,
        max_snapshot_bytes: int = 64 * 1024 * 1024,
        url_prefix: str = "/exports/download",
        cursor_secret: bytes | None = None,
        snapshot_dir: Path | None = None,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_lifetime_seconds = max_lifetime_seconds
        self.max_snapshot_bytes = max_snapshot_bytes
        self.url_prefix = url_prefix
        self._exports: dict[str, ExportEntry] = {}
        self._snapshots: dict[str, ExportSnapshot] = {}
        self._handles: dict[str, DownloadHandle] = {}
        self._expired_snapshots: set[str] = set()
        self._cursor_secret = cursor_secret or os.urandom(32)
        self._active_readers = 0
        self._closing = False
        self._snapshot_parent = snapshot_dir.resolve() if snapshot_dir else None
        if self._snapshot_parent:
            self._snapshot_parent.mkdir(parents=True, exist_ok=True)
            self._remove_abandoned_stores(self._snapshot_parent)
        self._temporary_root = tempfile.TemporaryDirectory(
            prefix=f"store-{os.getpid()}-",
            dir=str(self._snapshot_parent) if self._snapshot_parent else None,
        )
        self._storage_root = Path(self._temporary_root.name)

    @staticmethod
    def _process_is_running(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
            open_process.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            handle = open_process(0x1000, 0, pid)
            if handle:
                close_handle(handle)
                return True
            return ctypes.get_last_error() != 87
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    @classmethod
    def _remove_abandoned_stores(cls, root: Path) -> None:
        for prefix in ("store-", "ingest-"):
            for candidate in root.glob(f"{prefix}*"):
                parts = candidate.name.split("-", 2)
                if not candidate.is_dir() or len(parts) != 3 or parts[0] not in {"store", "ingest"} or not parts[1].isdigit():
                    continue
                if not cls._process_is_running(int(parts[1])):
                    shutil.rmtree(candidate)

    def close(self) -> None:
        self._closing = True
        for snapshot in list(self._snapshots.values()):
            self._retire_snapshot(snapshot)
        self._snapshots.clear()
        self._handles.clear()
        self._cleanup_storage_root_if_closed()

    def _cleanup_storage_root_if_closed(self) -> None:
        if self._closing and self._active_readers == 0:
            self._temporary_root.cleanup()

    @staticmethod
    def _remove_snapshot_dir(snapshot: ExportSnapshot) -> None:
        shutil.rmtree(snapshot.storage_dir, ignore_errors=True)

    def _retire_snapshot(self, snapshot: ExportSnapshot) -> None:
        snapshot.retired = True
        if snapshot.active_readers == 0:
            self._remove_snapshot_dir(snapshot)

    def _purge(self, now: float | None = None) -> None:
        current_time = time.time() if now is None else now
        expired_exports: set[str] = set()
        for key, entry in list(self._exports.items()):
            if entry.expires_at <= current_time:
                self._exports.pop(key, None)
                expired_exports.add(key)
        for key, snapshot in list(self._snapshots.items()):
            if snapshot.expires_at <= current_time or snapshot.absolute_expires_at <= current_time:
                self._snapshots.pop(key, None)
                self._retire_snapshot(snapshot)
                expired_exports.add(key)
                self._expired_snapshots.add(key)
        for key, handle in list(self._handles.items()):
            if handle.expires_at <= current_time or handle.export_id in expired_exports:
                self._handles.pop(key, None)

    def pop_expired_snapshots(self, *, now: float | None = None) -> list[str]:
        self._purge(now)
        expired = sorted(self._expired_snapshots)
        self._expired_snapshots.clear()
        return expired

    def put(self, export_id: str, payload: dict[str, Any], *, response: dict[str, Any] | None = None) -> float:
        self._purge()
        expires = time.time() + self.ttl_seconds
        self._exports[export_id] = ExportEntry(
            export_id,
            copy.deepcopy(payload),
            expires,
            copy.deepcopy(response) if response is not None else None,
        )
        return expires

    def get(self, export_id: str) -> dict[str, Any] | None:
        self._purge()
        entry = self._exports.get(export_id)
        return copy.deepcopy(entry.payload) if entry else None

    def get_response(self, export_id: str) -> dict[str, Any] | None:
        self._purge()
        entry = self._exports.get(export_id)
        if not entry:
            return None
        return copy.deepcopy(entry.response if entry.response is not None else entry.payload)

    def put_snapshot(
        self,
        export_id: str,
        *,
        session_id: str,
        conversation_url: str,
        payload: dict[str, Any],
        source: str,
        completeness: dict[str, str],
        reason: dict[str, str | None] | None = None,
        attachment_sources: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._purge()
        if self._closing:
            raise RuntimeError("export store is closing")
        now = time.time()
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else {}
        messages = manifest.get("messages", [])
        attachments = payload.get("attachments", [])
        if (
            not isinstance(messages, IterableABC)
            or isinstance(messages, (dict, str, bytes))
            or not isinstance(attachments, list)
        ):
            raise ValueError("export snapshot messages must be iterable and attachments must be a list")

        ordinal_by_id: dict[str, int] = {}
        for ordinal, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("id") or message.get("message_id") or "")
            if message_id:
                ordinal_by_id[message_id] = ordinal

        sources: dict[str, str] = {}
        for item in attachments:
            if not isinstance(item, dict):
                continue
            attachment_id = str(item.get("attachment_id") or "")
            source_url = str(item.get("source_url") or "")
            if attachment_id and source_url.startswith(("http://", "https://")) and _is_browser_download_host(source_url):
                sources[attachment_id] = source_url

        public_attachments: list[dict[str, Any]] = []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            attachment_id = str(item.get("attachment_id") or f"att_{uuid.uuid4().hex[:20]}")
            message_id = str(item.get("message_id") or "")
            has_source = attachment_id in sources
            is_candidate = has_source and bool(item.get("download_candidate"))
            available = has_source and not is_candidate
            public_attachments.append(
                {
                    "attachment_id": attachment_id,
                    "message_id": message_id or None,
                    "message_ordinal": ordinal_by_id.get(message_id),
                    "filename": item.get("filename") or "attachment",
                    "mime_type": item.get("mime_type"),
                    "file_kind": item.get("file_kind"),
                    "size": item.get("size"),
                    "downloadable": available and bool(item.get("downloadable")),
                    "available": available,
                    "download_candidate": is_candidate,
                    "unavailable_reason": None if available else item.get("unavailable_reason") or (
                        "ChatGPT file-card action must be tried" if is_candidate else "ChatGPT did not expose a downloadable URL"
                    ),
                }
            )

        public_attachments_by_message: dict[str, list[dict[str, Any]]] = {}
        for item in public_attachments:
            message_id = item.get("message_id")
            if message_id:
                public_attachments_by_message.setdefault(str(message_id), []).append(item)
        attachment_records: dict[str, dict[str, Any]] = {}
        for item in public_attachments:
            attachment_records.setdefault(str(item["attachment_id"]), item)
        retained_bytes = sum(
            sum(snapshot.view_sizes.values())
            for key, snapshot in self._snapshots.items()
            if key != export_id
        )
        stage_dir = self._storage_root / f".stage-{uuid.uuid4().hex}"
        storage_dir = self._storage_root / f"snapshot-{uuid.uuid4().hex}"
        stage_dir.mkdir()
        view_names = ("messages", "markdown", "attachments")
        stage_paths = {name: stage_dir / f"{name}.data" for name in view_names}
        view_sizes = {name: 0 for name in view_names}
        view_digests = {name: hashlib.sha256() for name in view_names}
        message_ranges: list[tuple[int, int]] = []
        message_ids: list[str | None] = []
        message_hashes: list[str] = []
        total_bytes = 0

        def write_chunk(output: Any, view: str, data: bytes, message_digest: Any = None) -> None:
            nonlocal total_bytes
            if retained_bytes + total_bytes + len(data) > self.max_snapshot_bytes:
                raise ExportCapacityError("export snapshot storage limit reached")
            output.write(data)
            view_digests[view].update(data)
            view_sizes[view] += len(data)
            total_bytes += len(data)
            if message_digest is not None:
                message_digest.update(data)

        def write_json(output: Any, view: str, value: Any, message_digest: Any = None) -> None:
            encoder = json.JSONEncoder(
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for part in encoder.iterencode(value):
                write_chunk(output, view, part.encode("utf-8"), message_digest)

        published = False
        try:
            with stage_paths["messages"].open("wb") as output:
                write_chunk(output, "messages", b"[")
                for ordinal, message in enumerate(messages):
                    if not isinstance(message, dict):
                        continue
                    if message_ranges:
                        write_chunk(output, "messages", b",")
                    public_message = {
                        str(key): _safe(_without_download_sources(value))
                        for key, value in message.items()
                        if key not in {"source_url", "download_url", "expires_at"}
                    }
                    public_message["ordinal"] = ordinal
                    message_id = str(message.get("id") or message.get("message_id") or "")
                    public_message["attachments"] = public_attachments_by_message.get(message_id, [])
                    public_message = _safe(public_message)
                    start = output.tell()
                    message_digest = hashlib.sha256()
                    write_json(output, "messages", public_message, message_digest)
                    end = output.tell()
                    message_ranges.append((start, end))
                    message_ids.append(message_id or None)
                    message_hashes.append(message_digest.hexdigest())
                write_chunk(output, "messages", b"]")

            with stage_paths["markdown"].open("wb") as output:
                markdown = payload.get("markdown")
                if isinstance(markdown, str):
                    markdown_parts: IterableABC = (markdown,)
                elif isinstance(markdown, IterableABC) and not isinstance(markdown, (dict, bytes)):
                    markdown_parts = markdown
                else:
                    markdown_parts = ()
                for part in markdown_parts:
                    if not isinstance(part, str):
                        raise ValueError("export Markdown stream must yield strings")
                    for offset in range(0, len(part), 64 * 1024):
                        write_chunk(output, "markdown", part[offset:offset + 64 * 1024].encode("utf-8"))

            with stage_paths["attachments"].open("wb") as output:
                write_json(output, "attachments", public_attachments)

            storage_dir.parent.mkdir(parents=True, exist_ok=True)
            stage_dir.rename(storage_dir)
            published = True
            snapshot = ExportSnapshot(
                export_id=export_id,
                session_id=session_id,
                conversation_url=conversation_url,
                source=source,
                completeness=self._normalize_completeness(completeness),
                reason=self._safe_reasons(reason or {}),
                storage_dir=storage_dir,
                view_paths={name: storage_dir / path.name for name, path in stage_paths.items()},
                view_sizes=view_sizes,
                view_hashes={name: digest.hexdigest() for name, digest in view_digests.items()},
                message_ranges=message_ranges,
                message_ids=message_ids,
                message_hashes=message_hashes,
                attachment_sources=sources,
                attachment_records=attachment_records,
                attachment_count=len(public_attachments),
                created_at=now,
                expires_at=now + self.ttl_seconds,
                absolute_expires_at=now + self.max_lifetime_seconds,
            )
            previous = self._snapshots.get(export_id)
            self._snapshots[export_id] = snapshot
            if previous:
                self._retire_snapshot(previous)
            for key, handle in list(self._handles.items()):
                if handle.export_id == export_id:
                    self._handles.pop(key, None)
            return self.snapshot_status(session_id, export_id)
        except Exception:
            shutil.rmtree(storage_dir if published else stage_dir, ignore_errors=True)
            raise

    @staticmethod
    def _normalize_completeness(value: dict[str, str]) -> dict[str, str]:
        states = {"verified", "unverified", "partial"}
        messages = value.get("messages") if value.get("messages") in states else "unverified"
        attachments = value.get("attachments") if value.get("attachments") in states else "unverified"
        overall = "verified" if messages == attachments == "verified" else (
            "partial" if "partial" in {messages, attachments} else "unverified"
        )
        return {"messages": messages, "attachments": attachments, "overall": overall}

    @staticmethod
    def _safe_reasons(value: dict[str, Any]) -> dict[str, str | None]:
        safe: dict[str, str | None] = {}
        for key in ("messages", "attachments"):
            reason = value.get(key)
            if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", reason):
                safe[key] = reason
            else:
                safe[key] = None
        return safe

    def _get_snapshot(self, session_id: str, export_id: str) -> ExportSnapshot:
        self._purge()
        snapshot = self._snapshots.get(export_id)
        if snapshot is None:
            raise KeyError("export snapshot not found or expired")
        if snapshot.session_id != session_id:
            raise ExportSessionMismatch("export belongs to a different session")
        for view, path in snapshot.view_paths.items():
            try:
                if path.stat().st_size != snapshot.view_sizes[view]:
                    raise OSError("export snapshot view size changed")
            except OSError as exc:
                self._snapshots.pop(export_id, None)
                self._retire_snapshot(snapshot)
                raise KeyError("export snapshot storage is missing or incomplete") from exc
        return snapshot

    @staticmethod
    def _read_view(snapshot: ExportSnapshot, view: str, offset: int, size: int) -> bytes:
        with snapshot.view_paths[view].open("rb") as stream:
            stream.seek(offset)
            data = stream.read(size)
        if len(data) != size:
            raise OSError("export snapshot view was truncated")
        return data

    def view_file(self, session_id: str, export_id: str, view: str) -> ExportViewLease:
        if view not in {"messages", "markdown", "attachments"}:
            raise ValueError("view must be messages, markdown, or attachments")
        snapshot = self._get_snapshot(session_id, export_id)
        snapshot.expires_at = min(time.time() + self.ttl_seconds, snapshot.absolute_expires_at)
        snapshot.active_readers += 1
        self._active_readers += 1
        return ExportViewLease(
            path=snapshot.view_paths[view],
            view=view,
            size=snapshot.view_sizes[view],
            sha256=snapshot.view_hashes[view],
            snapshot=snapshot,
        )

    def release_view_file(self, lease: ExportViewLease) -> None:
        snapshot = lease.snapshot
        if snapshot.active_readers <= 0 or self._active_readers <= 0:
            raise RuntimeError("export view lease was released more than once")
        snapshot.active_readers -= 1
        self._active_readers -= 1
        if snapshot.retired and snapshot.active_readers == 0:
            self._remove_snapshot_dir(snapshot)
        self._cleanup_storage_root_if_closed()

    def snapshot_status(self, session_id: str, export_id: str) -> dict[str, Any]:
        snapshot = self._get_snapshot(session_id, export_id)
        return {
            "session_id": snapshot.session_id,
            "export_id": snapshot.export_id,
            "state": "READY",
            "source": snapshot.source,
            "completeness": copy.deepcopy(snapshot.completeness),
            "reason": copy.deepcopy(snapshot.reason),
            "message_count": len(snapshot.message_ranges),
            "attachment_count": snapshot.attachment_count,
            "views": {
                name: {"total_bytes": snapshot.view_sizes[name], "sha256": snapshot.view_hashes[name]}
                for name in snapshot.view_paths
            },
            "created_at": _iso(snapshot.created_at),
            "expires_at": _iso(snapshot.expires_at),
        }

    def page(
        self,
        *,
        session_id: str,
        export_id: str,
        view: str,
        cursor: str | None,
        max_response_bytes: int = MAX_PAGE_RESPONSE_BYTES,
    ) -> dict[str, Any]:
        if view not in {"messages", "markdown", "attachments"}:
            raise ValueError("view must be messages, markdown, or attachments")
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool):
            raise PageLimitTooSmall("max_response_bytes must be an integer")
        if max_response_bytes < 1024 or max_response_bytes > MAX_PAGE_RESPONSE_BYTES:
            raise PageLimitTooSmall(
                f"max_response_bytes must be between 1024 and {MAX_PAGE_RESPONSE_BYTES}"
            )
        snapshot = self._get_snapshot(session_id, export_id)
        if view == "messages":
            return self._message_page(
                snapshot,
                cursor=cursor,
                max_response_bytes=max_response_bytes,
            )
        view_size = snapshot.view_sizes[view]
        offset = 0
        if cursor:
            token = self._decode_cursor(cursor)
            expected = {
                "e": export_id,
                "s": session_id,
                "v": view,
                "p": max_response_bytes,
                "d": snapshot.view_hashes[view],
            }
            if any(token.get(key) != value for key, value in expected.items()):
                raise InvalidExportCursor("cursor does not match this session, export, view, or page size")
            offset = token.get("o")
            if not isinstance(offset, int) or offset <= 0 or offset >= view_size:
                raise InvalidExportCursor("cursor offset is invalid")

        if not view_size:
            chunk = ""
            end = 0
        else:
            end = min(view_size, offset + max(1, max_response_bytes - 1400))
            while end > offset:
                try:
                    chunk = self._read_view(snapshot, view, offset, end - offset).decode("utf-8")
                    break
                except UnicodeDecodeError:
                    end -= 1
            if end <= offset:
                raise PageLimitTooSmall("response limit cannot fit one UTF-8 character and its envelope")
            while True:
                eof = end >= view_size
                next_cursor = None if eof else self._encode_cursor(
                    {
                        "e": export_id,
                        "s": session_id,
                        "v": view,
                        "p": max_response_bytes,
                        "d": snapshot.view_hashes[view],
                        "o": end,
                    }
                )
                result = self._page_response(snapshot, view, offset, chunk, next_cursor, eof)
                encoded_size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if encoded_size <= max_response_bytes:
                    break
                end = offset + max(0, (end - offset) // 2)
                while end > offset:
                    try:
                        chunk = self._read_view(snapshot, view, offset, end - offset).decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        end -= 1
                if end <= offset:
                    raise PageLimitTooSmall("response limit cannot fit one UTF-8 character and its envelope")

        now = time.time()
        snapshot.expires_at = min(now + self.ttl_seconds, snapshot.absolute_expires_at)
        if not view_size:
            result = self._page_response(snapshot, view, 0, "", None, True)
        else:
            result = self._page_response(snapshot, view, offset, chunk, next_cursor, end >= view_size)
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_response_bytes:
            raise PageLimitTooSmall("response envelope exceeds max_response_bytes")
        return result

    def _message_page(
        self,
        snapshot: ExportSnapshot,
        *,
        cursor: str | None,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        message_count = len(snapshot.message_ranges)
        ordinal = 0
        segment_offset = 0
        if cursor:
            token = self._decode_cursor(cursor)
            expected = {
                "e": snapshot.export_id,
                "s": snapshot.session_id,
                "v": "messages",
                "p": max_response_bytes,
                "d": snapshot.view_hashes["messages"],
                "m": 2,
            }
            if any(token.get(key) != value for key, value in expected.items()):
                raise InvalidExportCursor("cursor does not match this session, export, view, or page size")
            ordinal = token.get("o")
            segment_offset = token.get("b", 0)
            if (
                not isinstance(ordinal, int)
                or isinstance(ordinal, bool)
                or ordinal < 0
                or ordinal >= message_count
                or not isinstance(segment_offset, int)
                or isinstance(segment_offset, bool)
                or segment_offset < 0
            ):
                raise InvalidExportCursor("message cursor position is invalid")
            start, end = snapshot.message_ranges[ordinal]
            if segment_offset >= end - start:
                raise InvalidExportCursor("message cursor segment offset is invalid")

        now = time.time()
        snapshot.expires_at = min(now + self.ttl_seconds, snapshot.absolute_expires_at)
        if message_count == 0:
            return self._message_page_response(snapshot, 0, [], None, None, True)
        if segment_offset:
            return self._message_segment_page(
                snapshot,
                ordinal=ordinal,
                segment_offset=segment_offset,
                max_response_bytes=max_response_bytes,
            )

        start_ordinal = ordinal
        records: list[dict[str, Any]] = []
        accepted: dict[str, Any] | None = None
        while ordinal < message_count:
            start, end = snapshot.message_ranges[ordinal]
            if end - start > max_response_bytes - 900:
                if records:
                    break
                return self._message_segment_page(
                    snapshot,
                    ordinal=ordinal,
                    segment_offset=0,
                    max_response_bytes=max_response_bytes,
                )
            record = json.loads(self._read_view(snapshot, "messages", start, end - start))
            candidate_records = [*records, record]
            next_ordinal = ordinal + 1
            eof = next_ordinal == message_count
            next_cursor = None if eof else self._message_cursor(snapshot, max_response_bytes, next_ordinal, 0)
            candidate = self._message_page_response(
                snapshot,
                start_ordinal,
                candidate_records,
                None,
                next_cursor,
                eof,
            )
            encoded_size = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if encoded_size > max_response_bytes:
                if records:
                    break
                return self._message_segment_page(
                    snapshot,
                    ordinal=ordinal,
                    segment_offset=0,
                    max_response_bytes=max_response_bytes,
                )
            records = candidate_records
            ordinal = next_ordinal
            accepted = candidate
            if eof:
                break

        if accepted is None:
            raise PageLimitTooSmall("response limit cannot fit one message page")
        return accepted

    def _message_segment_page(
        self,
        snapshot: ExportSnapshot,
        *,
        ordinal: int,
        segment_offset: int,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        start, end = snapshot.message_ranges[ordinal]
        message_size = end - start
        message_id = snapshot.message_ids[ordinal]
        segment_end = min(message_size, segment_offset + max(1, max_response_bytes - 900))
        while segment_end > segment_offset:
            try:
                chunk_bytes = self._read_view(
                    snapshot,
                    "messages",
                    start + segment_offset,
                    segment_end - segment_offset,
                )
                chunk = chunk_bytes.decode("utf-8")
                break
            except UnicodeDecodeError:
                segment_end -= 1
        else:
            raise PageLimitTooSmall("response limit cannot fit one UTF-8 character and its envelope")

        while True:
            complete = segment_end == message_size
            next_ordinal = ordinal + 1 if complete else ordinal
            next_offset = 0 if complete else segment_end
            eof = complete and next_ordinal == len(snapshot.message_ranges)
            next_cursor = (
                None
                if eof
                else self._message_cursor(snapshot, max_response_bytes, next_ordinal, next_offset)
            )
            result = self._message_page_response(
                snapshot,
                ordinal,
                [],
                {
                    "ordinal": ordinal,
                    "message_id": message_id,
                    "encoding": "utf-8-json",
                    "chunk": chunk,
                    "byte_offset": segment_offset,
                    "returned_bytes": len(chunk_bytes),
                    "total_bytes": message_size,
                    "chunk_sha256": hashlib.sha256(chunk_bytes).hexdigest(),
                    "sha256": snapshot.message_hashes[ordinal],
                    "complete": complete,
                },
                next_cursor,
                eof,
            )
            if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= max_response_bytes:
                return result
            segment_end = segment_offset + max(0, (segment_end - segment_offset) // 2)
            while segment_end > segment_offset:
                try:
                    chunk_bytes = self._read_view(
                        snapshot,
                        "messages",
                        start + segment_offset,
                        segment_end - segment_offset,
                    )
                    chunk = chunk_bytes.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    segment_end -= 1
            if segment_end <= segment_offset:
                raise PageLimitTooSmall("response limit cannot fit one UTF-8 character and its envelope")

    def _message_cursor(
        self,
        snapshot: ExportSnapshot,
        max_response_bytes: int,
        ordinal: int,
        segment_offset: int,
    ) -> str:
        return self._encode_cursor(
            {
                "e": snapshot.export_id,
                "s": snapshot.session_id,
                "v": "messages",
                "p": max_response_bytes,
                "d": snapshot.view_hashes["messages"],
                "m": 2,
                "o": ordinal,
                "b": segment_offset,
            }
        )

    @staticmethod
    def _message_page_response(
        snapshot: ExportSnapshot,
        start_ordinal: int,
        messages: list[dict[str, Any]],
        message_segment: dict[str, Any] | None,
        next_cursor: str | None,
        eof: bool,
    ) -> dict[str, Any]:
        return {
            "session_id": snapshot.session_id,
            "export_id": snapshot.export_id,
            "view": "messages",
            "start_ordinal": start_ordinal,
            "messages": messages,
            "message_segment": message_segment,
            "message_count": len(snapshot.message_ranges),
            "total_bytes": snapshot.view_sizes["messages"],
            "sha256": snapshot.view_hashes["messages"],
            "next_cursor": next_cursor,
            "eof": eof,
            "expires_at": _iso(snapshot.expires_at),
        }

    def view_bytes(self, session_id: str, export_id: str, view: str) -> bytes:
        if view not in {"messages", "markdown", "attachments"}:
            raise ValueError("view must be messages, markdown, or attachments")
        snapshot = self._get_snapshot(session_id, export_id)
        now = time.time()
        snapshot.expires_at = min(now + self.ttl_seconds, snapshot.absolute_expires_at)
        return self._read_view(snapshot, view, 0, snapshot.view_sizes[view])

    @staticmethod
    def _page_response(
        snapshot: ExportSnapshot,
        view: str,
        offset: int,
        chunk: str,
        next_cursor: str | None,
        eof: bool,
    ) -> dict[str, Any]:
        data = chunk.encode("utf-8")
        return {
            "session_id": snapshot.session_id,
            "export_id": snapshot.export_id,
            "view": view,
            "chunk": chunk,
            "byte_offset": offset,
            "returned_bytes": len(data),
            "chunk_sha256": hashlib.sha256(data).hexdigest(),
            "total_bytes": snapshot.view_sizes[view],
            "sha256": snapshot.view_hashes[view],
            "next_cursor": next_cursor,
            "eof": eof,
            "expires_at": _iso(snapshot.expires_at),
        }

    def _encode_cursor(self, value: dict[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, token: str) -> dict[str, Any]:
        try:
            encoded = token.encode("ascii")
            padded = encoded + b"=" * (-len(encoded) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            if len(decoded) <= hashlib.sha256().digest_size:
                raise ValueError("cursor is too short")
            payload = decoded[:-hashlib.sha256().digest_size]
            signature = decoded[-hashlib.sha256().digest_size:]
            expected = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            value = json.loads(payload)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidExportCursor("cursor is malformed or tampered") from exc
        if not isinstance(value, dict):
            raise InvalidExportCursor("cursor payload is invalid")
        return value

    def attachment_handle(self, session_id: str, export_id: str, attachment_id: str) -> DownloadHandle:
        snapshot = self._get_snapshot(session_id, export_id)
        snapshot.expires_at = min(time.time() + self.ttl_seconds, snapshot.absolute_expires_at)
        source_url = snapshot.attachment_sources.get(attachment_id)
        if not source_url:
            raise AttachmentUnavailable("attachment is not available through the logged-in ChatGPT page")
        return self.add_handle(
            export_id,
            attachment_id,
            source_url,
            session_id=session_id,
            conversation_url=snapshot.conversation_url,
        )

    def attachment_source(
        self, session_id: str, export_id: str, attachment_id: str
    ) -> tuple[dict[str, Any], str, str]:
        snapshot = self._get_snapshot(session_id, export_id)
        snapshot.expires_at = min(time.time() + self.ttl_seconds, snapshot.absolute_expires_at)
        attachment = snapshot.attachment_records.get(attachment_id)
        source_url = snapshot.attachment_sources.get(attachment_id)
        if (
            attachment is None
            or not source_url
            or not (attachment.get("available") or attachment.get("download_candidate"))
        ):
            raise AttachmentUnavailable("attachment is not available through the logged-in ChatGPT page")
        return attachment, source_url, snapshot.conversation_url

    def release_snapshot(self, session_id: str, export_id: str) -> None:
        snapshot = self._get_snapshot(session_id, export_id)
        self._snapshots.pop(export_id, None)
        self._retire_snapshot(snapshot)
        for key, handle in list(self._handles.items()):
            if handle.export_id == export_id:
                self._handles.pop(key, None)

    def revoke_session(self, session_id: str) -> list[str]:
        revoked: list[str] = []
        for export_id, snapshot in list(self._snapshots.items()):
            if snapshot.session_id == session_id:
                self._snapshots.pop(export_id, None)
                self._retire_snapshot(snapshot)
                revoked.append(export_id)
        for key, handle in list(self._handles.items()):
            if handle.session_id == session_id:
                self._handles.pop(key, None)
        return revoked

    def add_handle(
        self,
        export_id: str,
        attachment_id: str,
        source_url: str,
        *,
        session_id: str | None = None,
        conversation_url: str | None = None,
    ) -> DownloadHandle:
        self._purge()
        expires_at = time.time() + self.ttl_seconds
        if session_id is not None:
            snapshot = self._get_snapshot(session_id, export_id)
            expires_at = min(expires_at, snapshot.absolute_expires_at)
        handle = DownloadHandle(
            handle=f"dl_{uuid.uuid4().hex[:32]}",
            export_id=export_id,
            attachment_id=attachment_id,
            source_url=source_url,
            expires_at=expires_at,
            session_id=session_id,
            conversation_url=conversation_url,
        )
        self._handles[handle.handle] = handle
        return handle

    def consume(self, handle_id: str) -> DownloadHandle:
        self._purge()
        handle = self._handles.get(handle_id)
        if handle is None:
            raise KeyError("download handle not found or expired")
        if handle.consumed:
            raise KeyError("download handle has already been consumed")
        handle.consumed = True
        return handle

    def handle(self, handle_id: str) -> DownloadHandle | None:
        self._purge()
        return self._handles.get(handle_id)


def make_export(
    *,
    export_id: str,
    conversation_url: str,
    scope: str,
    count: int,
    browser_payload: dict[str, Any] | list[dict[str, Any]],
    handles: ExportStore,
    download_url: Callable[[DownloadHandle], str] | None = None,
    create_download_handles: bool = True,
) -> dict[str, Any]:
    """Build a safe manifest + Markdown response from browser extraction data."""
    if scope not in {"recent", "all"}:
        raise ValueError("scope must be recent or all")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise ValueError("count must be a positive integer")
    if isinstance(browser_payload, list):
        raw_messages = browser_payload
        raw_attachments: Any = []
    else:
        raw_messages = browser_payload.get("messages", [])
        raw_attachments = browser_payload.get("attachments", [])
    streamed_messages = bool(getattr(raw_messages, "is_message_spool", False))
    messages = raw_messages if streamed_messages else _normalise_messages(raw_messages)
    selected = messages if scope == "all" else recent_turns(messages, count)
    selected_is_spooled = streamed_messages and scope == "all"
    allowed_ids = {str(item.get("id")) for item in selected}
    attachments: list[dict[str, Any]] = []
    attachment_items = list(raw_attachments) if isinstance(raw_attachments, list) else []
    asset_pointer_ids: dict[str, set[str]] = {}
    for message in selected:
        message_id = str(message.get("id") or "")
        if not message_id:
            continue
        for part in message.get("rich_parts") or []:
            if not isinstance(part, dict) or part.get("content_type") not in {
                "image_asset_pointer",
                "simple_image_asset_pointer",
            }:
                continue
            pointer = part.get("asset_pointer")
            if not isinstance(pointer, str):
                continue
            tail = pointer.split("://", 1)[-1]
            pointer_id = next((segment for segment in tail.split("#") if segment.startswith("file_")), None)
            if pointer_id and re.fullmatch(r"file_[^/\\?#\x00-\x20]{1,256}", pointer_id):
                asset_pointer_ids.setdefault(message_id, set()).add(pointer_id)

            if pointer_id:
                attachment_items.append(
                    {
                        "attachment_id": pointer_id,
                        "id": pointer_id,
                        "file_id": pointer_id,
                        "message_id": message_id,
                        "file_kind": str(part.get("content_type")),
                    }
                )

    # Union conversation-level and message-level references. Dedupe by source/id
    # later so an inline asset pointer can enrich a metadata-only reference.
    for message in selected:
        for item in message.get("attachments") or []:
            if isinstance(item, dict):
                attachment_items.append({**item, "message_id": item.get("message_id") or message.get("id")})

    seen: dict[tuple[str, str], int] = {}
    for index, item in enumerate(attachment_items):
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("message_id") or item.get("messageId") or "")
        if message_id and message_id not in allowed_ids:
            continue
        source = item.get("source_url") or item.get("sourceUrl") or item.get("url") or item.get("href")
        source_url = str(source or "")
        if source_url.startswith("/"):
            source_url = urljoin(conversation_url, source_url)
        file_id = str(item.get("file_id") or item.get("fileId") or "")
        attachment_ref = str(item.get("attachment_id") or item.get("id") or "")
        pointer_match = bool(
            message_id
            and attachment_ref
            and attachment_ref in asset_pointer_ids.get(message_id, set())
        )
        if not file_id and pointer_match:
            file_id = attachment_ref
        download_candidate = False
        observed_asset_pointer = pointer_match or item.get("file_kind") in {
            "image_asset_pointer",
            "simple_image_asset_pointer",
        }
        card_action_evidence = item.get("downloadable") is True or (
            item.get("downloadable") is None
            and not item.get("library_file_id")
            and not observed_asset_pointer
        )
        if not source_url and file_id and card_action_evidence:
            conversation = urlparse(conversation_url)
            conversation_id = conversation.path.rstrip("/").rsplit("/", 1)[-1]
            source_url = (
                f"{conversation.scheme}://{conversation.netloc}/backend-api/files/download/"
                f"{quote(file_id, safe='')}?conversation_id={quote(conversation_id, safe='')}&inline=true"
            )
            download_candidate = item.get("downloadable") is not True
        file_kind = item.get("file_kind") or (
            "library_file"
            if item.get("library_file_id")
            else "image_asset_pointer"
            if observed_asset_pointer
            else "api_attachment"
            if file_id or attachment_ref
            else "attachment"
        )
        identity = str(
            item.get("library_file_id")
            or file_id
            or attachment_ref
            or source_url
            or index
        )
        key = (message_id, str(file_kind), identity)
        if key in seen:
            existing = attachments[seen[key]]
            if file_id and not existing.get("file_id"):
                existing["file_id"] = file_id
            for name in ("filename", "mime_type", "file_kind", "size"):
                value = item.get(name) or item.get({"filename": "file_name", "mime_type": "mimeType", "size": "file_size_bytes"}.get(name, ""))
                if name == "size" and observed_asset_pointer:
                    value = None
                if value is not None and (not existing.get(name) or name == "filename" and existing.get(name) == "attachment"):
                    existing[name] = value
            if observed_asset_pointer:
                existing["size"] = None
            source_is_allowed = (
                source_url.startswith(("http://", "https://"))
                and _is_browser_download_host(source_url)
            )
            source_is_better = source_is_allowed and (
                not existing.get("source_url")
                or (existing.get("download_candidate") and not download_candidate)
            )
            if source_is_better:
                existing["source_url"] = source_url
                existing["download_candidate"] = download_candidate
                existing["downloadable"] = not download_candidate
                if download_candidate:
                    existing["unavailable_reason"] = "ChatGPT file-card action has not been verified"
                else:
                    existing.pop("unavailable_reason", None)
                if create_download_handles:
                    handle = handles.add_handle(
                        export_id,
                        str(existing["attachment_id"]),
                        source_url,
                        conversation_url=conversation_url,
                    )
                    existing["expires_at"] = _iso(handle.expires_at)
                    existing["download_url"] = (download_url or (lambda value: value.public_url(handles.url_prefix)))(handle)
                else:
                    existing["download_url"] = None
            continue
        seen[key] = len(attachments)
        attachment_id = str(
            item.get("attachment_id")
            or item.get("library_file_id")
            or item.get("id")
            or f"att_{uuid.uuid4().hex[:20]}"
        )
        if item.get("library_file_id") and attachment_id == str(item.get("id") or ""):
            attachment_id = str(item["library_file_id"])
        record: dict[str, Any] = {
            "attachment_id": attachment_id,
            "message_id": message_id or None,
            "file_id": file_id or None,
            "filename": str(item.get("filename") or item.get("file_name") or item.get("name") or "attachment"),
            "mime_type": item.get("mime_type") or item.get("mimeType"),
            "file_kind": file_kind if file_kind != "attachment" else None,
            "downloadable": False,
            "download_candidate": download_candidate,
            "size": None if observed_asset_pointer else item.get("size"),
            "source_url": source_url or None,
        }
        if source_url.startswith(("http://", "https://")) and _is_browser_download_host(source_url):
            if download_candidate:
                record["downloadable"] = False
                record["unavailable_reason"] = "ChatGPT file-card action has not been verified"
            else:
                record["downloadable"] = True
            if create_download_handles:
                handle = handles.add_handle(
                    export_id,
                    attachment_id,
                    source_url,
                    conversation_url=conversation_url,
                )
                record["expires_at"] = _iso(handle.expires_at)
                record["download_url"] = (download_url or (lambda value: value.public_url(handles.url_prefix)))(handle)
            else:
                record["download_url"] = None
        else:
            record["download_url"] = None
            record["unavailable_reason"] = str(
                item.get("unavailable_reason")
                or (
                    "Resource URL is outside the logged-in ChatGPT download scope"
                    if source_url
                else "ChatGPT asset pointer has no verified download source"
                if observed_asset_pointer
                else "ChatGPT library reference has no verified download source"
                if item.get("library_file_id")
                else "ChatGPT API attachment reference has no verified download source"
                if file_id or attachment_ref
                else "ChatGPT did not expose a downloadable URL"
            )
            )
        attachments.append(_safe(record))
    # Add the generated attachment handle to each message-level association,
    # while retaining the top-level attachment index for easy client lookup.
    by_message: dict[str, list[dict[str, Any]]] = {}
    for attachment in attachments:
        message_id = attachment.get("message_id")
        if message_id:
            by_message.setdefault(str(message_id), []).append(
                {
                    "attachment_id": attachment.get("attachment_id"),
                    "file_id": attachment.get("file_id"),
                    "filename": attachment.get("filename"),
                    "mime_type": attachment.get("mime_type"),
                    "file_kind": attachment.get("file_kind"),
                    "downloadable": attachment.get("downloadable"),
                    "size": attachment.get("size"),
                    "source_url": attachment.get("source_url"),
                    "download_url": attachment.get("download_url"),
                    "expires_at": attachment.get("expires_at"),
                }
            )
    if not selected_is_spooled:
        for message in selected:
            if message.get("id") in by_message:
                message["attachments"] = by_message[str(message["id"])]

    manifest = {
        "conversation_url": conversation_url,
        "messages": selected,
        "attachments": attachments,
    }
    payload = {
        "export_id": export_id,
        "conversation_url": conversation_url,
        "scope": scope,
        "message_count": len(selected),
        "manifest": manifest,
        "markdown": _MarkdownStream(selected, by_message) if selected_is_spooled else _markdown(selected),
        "attachments": attachments,
    }
    return payload


__all__ = ["ExportStore", "DownloadHandle", "make_export", "recent_turns"]
