from __future__ import annotations

import asyncio
import math
import os
import re
import subprocess
import uuid
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    async_playwright,
)

from .config import Settings, default_chrome_executable
from .spooling import PagedMessageSpool
from .models import utc_now
from .file_store import FileRecord
from .uploads import InMemoryUpload


class BrowserNeedsUserAction(RuntimeError):
    def __init__(self, reason: str, instructions: str):
        super().__init__(reason)
        self.reason = reason
        self.instructions = instructions


class BrowserTaskError(RuntimeError):
    pass


@dataclass(frozen=True)
class StreamedAttachment:
    size: int
    mime_type: str | None
    content_disposition: str | None


def _is_explicit_login_page(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.hostname == "auth.openai.com":
        return True
    return parsed.path.rstrip("/").lower() in {"/auth/login", "/login", "/log-in", "/sign-in"}


def _is_chatgpt_download_host(host: str) -> bool:
    return host in {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
        "www.chat.openai.com",
        "oaiusercontent.com",
    } or host.endswith((".oaiusercontent.com", ".openai.com"))


def _conversation_messages_request(
    conversation_url: str,
    conversation_id: str,
    latest_request_url: str,
    cursor: str,
    request_headers: dict[str, str],
) -> tuple[str, dict[str, str]]:
    target = urlparse(conversation_url)
    latest = urlparse(latest_request_url)
    if target.path.rstrip("/").rsplit("/", 1)[-1] != conversation_id:
        raise ValueError("conversation URL does not match the requested ID")
    expected_path = f"/backend-api/conversations/{conversation_id}"
    if latest.path != expected_path:
        raise ValueError("latest API request does not match the target conversation")
    if latest.hostname not in {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
        "www.chat.openai.com",
    }:
        raise ValueError("latest API request is outside ChatGPT")
    if not cursor:
        raise ValueError("older-page cursor is empty")

    original_query = parse_qs(latest.query)
    if "include_has_versions" not in original_query:
        raise ValueError("latest API request omitted include_has_versions")
    query = {
        "before": cursor,
        "include_has_versions": original_query["include_has_versions"][-1],
    }
    if original_query.get("num_turns"):
        query["num_turns"] = original_query["num_turns"][-1]

    message_path = f"/backend-api/conversations/{quote(conversation_id, safe='')}/messages"
    headers = _client_api_headers(request_headers, message_path)

    return (
        f"{latest.scheme}://{latest.netloc}{message_path}?{urlencode(query)}",
        headers,
    )


def _client_api_headers(
    request_headers: dict[str, str],
    target_path: str,
) -> dict[str, str]:
    headers = {
        name.lower(): value
        for name, value in request_headers.items()
        if name.lower() == "authorization"
        or name.lower().startswith(("oai-", "x-oai-", "x-openai-"))
    }
    headers["x-openai-target-path"] = target_path
    headers["x-openai-target-route"] = target_path

    return headers


async def _authenticated_page_json_get(
    page: Page,
    request_url: str,
    client_headers: dict[str, str],
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    auth_refreshed = False
    for retry_index in range(3):
        try:
            response = await page.evaluate(
                """async ({url, headers}) => {
                    const response = await fetch(url, {
                        method: "GET",
                        credentials: "include",
                        headers
                    });
                    let payload = null;
                    if (response.ok) {
                        try {
                            payload = await response.json();
                        } catch {
                            payload = null;
                        }
                    }
                    return {
                        status: response.status,
                        retry_after: response.headers.get("retry-after"),
                        payload
                    };
                }""",
                {"url": request_url, "headers": client_headers},
            )
        except PlaywrightError:
            attempts.append({"status": None, "retry_attempt": retry_index + 1, "error_type": "page_request_failed"})
            return {"status": None, "payload": None, "attempts": attempts}

        status = response.get("status") if isinstance(response, dict) else None
        if status in {401, 403} and not auth_refreshed:
            try:
                access_token = await page.evaluate(
                    """async () => {
                        const response = await fetch("/api/auth/session", {
                            credentials: "include"
                        });
                        if (!response.ok) return null;
                        const session = await response.json();
                        return typeof session?.accessToken === "string"
                            ? session.accessToken
                            : null;
                    }"""
                )
            except PlaywrightError:
                access_token = None
            auth_refreshed = True
            if isinstance(access_token, str) and access_token:
                attempts.append(
                    {"status": status, "retry_attempt": retry_index + 1, "error_type": "authentication_retry"}
                )
                client_headers["authorization"] = f"Bearer {access_token}"
                continue

        if status == 429 and retry_index < 2:
            attempts.append(
                {"status": status, "retry_attempt": retry_index + 1, "error_type": "rate_limited_retry"}
            )
            retry_after = response.get("retry_after")
            try:
                delay = float(retry_after) if retry_after is not None else 2 ** retry_index
            except (TypeError, ValueError):
                delay = 2 ** retry_index
            if not math.isfinite(delay):
                delay = 2 ** retry_index
            await asyncio.sleep(min(max(delay, 0.5), 30.0))
            continue

        if not isinstance(status, int) or status != 200:
            attempts.append(
                {
                    "status": status if isinstance(status, int) else None,
                    "retry_attempt": retry_index + 1,
                    "error_type": "http_error",
                }
            )
            return {"status": status, "payload": None, "attempts": attempts}

        payload = response.get("payload") if isinstance(response, dict) else None
        if payload is None:
            attempts.append(
                {"status": status, "retry_attempt": retry_index + 1, "error_type": "invalid_json"}
            )
            return {"status": status, "payload": None, "attempts": attempts}

        attempts.append({"status": status, "retry_attempt": retry_index + 1})
        return {"status": status, "payload": payload, "attempts": attempts}

    return {"status": None, "payload": None, "attempts": attempts}


def _has_authenticated_conversation_response(page_events: list[dict[str, Any]]) -> bool:
    return any(
        event.get("direction") == "latest"
        and isinstance(event.get("payload"), dict)
        and isinstance(event["payload"].get("messages"), list)
        for event in page_events
    )


async def _collect_older_api_pages(
    latest_payload: dict[str, Any],
    fetch_page: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]],
    on_page: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    pages: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    current = latest_payload
    while True:
        info = current.get("page_info") if isinstance(current.get("page_info"), dict) else {}
        has_previous = info.get("has_previous_page")
        if has_previous is False:
            return pages, False
        if has_previous is not True:
            return pages, True
        cursor = info.get("start_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            return pages, True
        seen_cursors.add(cursor)
        page = await fetch_page(current)
        if not isinstance(page, dict) or page.get("cursor_matches") is not True:
            return pages, True
        payload = page.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return pages, True
        if on_page is None:
            pages.append(page)
        else:
            await on_page(page)
            pages.append(
                {
                    "direction": page.get("direction"),
                    "request_cursor": page.get("request_cursor"),
                    "cursor_matches": page.get("cursor_matches"),
                    "payload": {"messages": [], "page_info": payload.get("page_info")},
                }
            )
        current = payload


def _attachment_scan_reaches_api_count(scan: dict[str, Any], expected_message_count: int) -> bool:
    observed_count = scan.get("observed_message_id_count")
    return type(observed_count) is int and observed_count >= expected_message_count


def _api_attachment_reference_summary(
    messages: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    library_items = [item for item in attachments if isinstance(item.get("library_file_id"), str) and item["library_file_id"]]
    library_ids = {str(item["library_file_id"]) for item in library_items}
    attachment_file_ids = {
        str(value)
        for item in attachments
        for value in (item.get("file_id"), item.get("fileId"))
        if isinstance(value, str) and value
    }
    pointer_ids = {
        file_id
        for message in messages
        for part in message.get("rich_parts") or []
        if isinstance(part, dict)
        for file_id in [_asset_pointer_file_id(part)]
        if file_id
    }
    rich_parts = [
        part
        for message in messages
        for part in message.get("rich_parts") or []
        if isinstance(part, dict)
    ]
    content_types: dict[str, int] = {}
    for part in rich_parts:
        content_type = part.get("content_type")
        if isinstance(content_type, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", content_type):
            content_types[content_type] = content_types.get(content_type, 0) + 1
    return {
        "attachment_reference_count": len(attachments),
        "attachment_file_id_count": sum(bool(item.get("file_id") or item.get("fileId")) for item in attachments),
        "attachment_sandbox_path_count": sum(bool(item.get("sandbox_path")) for item in attachments),
        "library_file_id_count": len(library_items),
        "library_file_id_with_file_id_count": sum(
            bool(item.get("file_id") or item.get("fileId")) for item in library_items
        ),
        "library_file_id_matching_file_id_count": sum(
            str(item.get("library_file_id")) in {
                str(item.get("file_id") or item.get("fileId") or "")
            }
            for item in library_items
        ),
        "library_file_id_pointer_overlap_count": len(library_ids & pointer_ids),
        "file_id_pointer_overlap_count": len(attachment_file_ids & pointer_ids),
        "pointer_reference_count": len(pointer_ids),
        "library_file_id_with_sandbox_path_count": sum(bool(item.get("sandbox_path")) for item in library_items),
        "rich_part_count": len(rich_parts),
        "rich_part_file_id_count": sum(bool(part.get("file_id") or part.get("fileId")) for part in rich_parts),
        "rich_part_sandbox_path_count": sum(bool(part.get("sandbox_path")) for part in rich_parts),
        "rich_part_content_type_counts": dict(sorted(content_types.items())),
    }


def _interpreter_metadata_resource_urls(
    resource_urls: list[str],
    conversation_url: str,
    conversation_id: str,
) -> list[str]:
    target = urlparse(conversation_url)
    expected_path = f"/backend-api/conversation/{conversation_id}/interpreter/download"
    result: list[str] = []
    seen: set[tuple[str, str]] = set()
    for value in resource_urls:
        if not isinstance(value, str) or not value:
            continue
        source_url = urljoin(conversation_url, value)
        source = urlparse(source_url)
        query = parse_qs(source.query)
        message_id = (query.get("message_id") or [None])[0]
        sandbox_path = (query.get("sandbox_path") or [None])[0]
        if (
            source.scheme != "https"
            or source.netloc.lower() != target.netloc.lower()
            or source.username is not None
            or source.password is not None
            or source.path != expected_path
            or not set(query).issubset({"message_id", "sandbox_path"})
            or not message_id
            or not sandbox_path
        ):
            continue
        identity = (message_id, sandbox_path)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(source_url)
    return result


_SANDBOX_FILE_LINK = re.compile(r"sandbox:(/mnt/data/[^\s\]\)<>\"']+)", re.I)


def _sandbox_attachment_references(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"assistant", "tool"}:
            continue
        message_id = str(message.get("id") or message.get("message_id") or "")
        text = message.get("text")
        if not message_id or not isinstance(text, str):
            continue
        for match in _SANDBOX_FILE_LINK.finditer(text):
            sandbox_path = match.group(1).rstrip(".,;:")
            segments = sandbox_path.split("/")
            if (
                len(sandbox_path) > 1024
                or "\\" in sandbox_path
                or "\x00" in sandbox_path
                or any(segment in {".", ".."} for segment in segments)
            ):
                continue
            identity = (message_id, sandbox_path)
            if identity not in seen:
                seen.add(identity)
                result.append(identity)
    return result


def _asset_pointer_file_id(part: dict[str, Any]) -> str | None:
    if part.get("content_type") not in {"image_asset_pointer", "simple_image_asset_pointer"}:
        return None
    pointer = part.get("asset_pointer")
    if not isinstance(pointer, str):
        return None
    tail = pointer.split("://", 1)[-1]
    file_id = next((segment for segment in tail.split("#") if segment.startswith("file_")), None)
    return file_id if file_id and re.fullmatch(r"file_[^/\\?#\x00-\x20]{1,256}", file_id) else None


def _is_observed_file_download_url(source_url: str, file_id: str) -> bool:
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
        "www.chat.openai.com",
    }:
        return False
    if unquote(parsed.path.rsplit("/", 1)[-1]) != file_id:
        return False
    if parsed.path != f"/backend-api/files/download/{quote(file_id, safe='')}" or not parsed.query:
        return False
    query = parse_qs(parsed.query)
    return (
        set(query).issubset({"download_intent", "inline", "post_id"})
        and query.get("download_intent") == ["false"]
        and query.get("inline") == ["false"]
    )


def _api_attachment_metadata_error(
    source_url: str,
    conversation_url: str,
    attachment_id: str,
    filename: str,
    size: int,
    payload: Any,
) -> str | None:
    target = urlparse(conversation_url)
    source = urlparse(source_url)
    conversation_id = target.path.rstrip("/").rsplit("/", 1)[-1]
    expected_path = f"/backend-api/files/download/{quote(attachment_id, safe='')}"
    query = parse_qs(source.query)
    if (
        source.scheme != "https"
        or source.netloc.lower() != target.netloc.lower()
        or source.path != expected_path
        or set(query) != {"conversation_id", "inline"}
        or query.get("conversation_id") != [conversation_id]
        or query.get("inline") != ["true"]
    ):
        return "source_route_unverified"
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return "metadata_unavailable"
    returned_size = payload.get("file_size_bytes")
    if (
        payload.get("file_name") != filename
        or type(returned_size) is not int
        or returned_size != size
    ):
        return "reference_metadata_mismatch"
    download_url = payload.get("download_url")
    if not isinstance(download_url, str) or not download_url:
        return "download_url_unavailable"
    final = urlparse(urljoin(source_url, download_url))
    if (
        final.scheme != "https"
        or not _is_chatgpt_download_host(final.hostname or "")
        or final.path != "/backend-api/estuary/content"
    ):
        return "content_route_unverified"
    if (parse_qs(final.query).get("id") or [None])[0] != attachment_id:
        return "content_id_mismatch"
    return None


def _file_download_resource_urls(
    resource_urls: list[str],
    conversation_url: str,
    referenced_file_ids: set[str],
) -> list[str]:
    target = urlparse(conversation_url)
    result: list[str] = []
    seen_file_ids: set[str] = set()
    for value in resource_urls:
        if not isinstance(value, str) or not value:
            continue
        source_url = urljoin(conversation_url, value)
        source = urlparse(source_url)
        file_id = unquote(source.path.rsplit("/", 1)[-1])
        if (
            source.scheme != "https"
            or source.netloc.lower() != target.netloc.lower()
            or source.username is not None
            or source.password is not None
            or file_id not in referenced_file_ids
            or file_id in seen_file_ids
            or not _is_observed_file_download_url(source_url, file_id)
        ):
            continue
        seen_file_ids.add(file_id)
        result.append(source_url)
    return result


def _file_download_attachments_from_responses(
    messages: list[dict[str, Any]],
    responses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs_by_file_id: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}

    def add_ref(file_id: str, message_id: str, item: dict[str, Any]) -> None:
        attachment_id = str(item.get("attachment_id") or item.get("id") or file_id)
        key = (message_id, attachment_id)
        refs = refs_by_file_id.setdefault(file_id, {})
        current = refs.setdefault(key, {"attachment_id": attachment_id, "file_id": file_id, "message_id": message_id})
        for name in ("filename", "mime_type", "size", "file_kind"):
            if item.get(name) is not None and current.get(name) is None:
                current[name] = item[name]

    for message in messages:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("id") or message.get("message_id") or "")
        if not message_id:
            continue
        for item in message.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            # This route only verifies pointer bytes, not the library original.
            if item.get("library_file_id") or item.get("libraryFileId"):
                continue
            for value in (item.get("file_id"), item.get("fileId"), item.get("library_file_id"), item.get("attachment_id"), item.get("id")):
                if isinstance(value, str) and value:
                    reference = item
                    file_identity_fields = {
                        str(identity)
                        for identity in (item.get("file_id"), item.get("fileId"), item.get("library_file_id"))
                        if isinstance(identity, str) and identity
                    }
                    if value not in file_identity_fields:
                        reference = {**item, "attachment_id": value}
                    add_ref(value, message_id, reference)
        for part in message.get("rich_parts") or []:
            if not isinstance(part, dict):
                continue
            file_id = _asset_pointer_file_id(part)
            if file_id:
                add_ref(
                    file_id,
                    message_id,
                    {
                        "attachment_id": file_id,
                        "file_id": file_id,
                        "filename": part.get("filename"),
                        "mime_type": part.get("mime_type"),
                        "file_kind": part.get("content_type"),
                    },
                )

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for response in responses:
        file_id = str(response.get("file_id") or "")
        source_url = str(response.get("source_url") or "")
        if (
            not file_id
            or response.get("status") != "success"
            or not response.get("has_download_url")
            or not _is_observed_file_download_url(source_url, file_id)
        ):
            continue
        for ref in refs_by_file_id.get(file_id, {}).values():
            key = (str(ref.get("message_id") or ""), str(ref.get("attachment_id") or file_id))
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    **ref,
                    "filename": response.get("filename") or ref.get("filename") or "attachment",
                    "mime_type": response.get("mime_type") or ref.get("mime_type"),
                    "size": (
                        None
                        if ref.get("file_kind") in {"image_asset_pointer", "simple_image_asset_pointer"}
                        else response.get("size") or ref.get("size")
                    ),
                    "source_url": source_url,
                    "downloadable": True,
                    "download_candidate": False,
                    "unavailable_reason": None,
                }
            )
    return result


class ModelUnavailableError(BrowserTaskError):
    def __init__(self, requested: str, available: list[str], message: str | None = None):
        self.requested = requested
        self.available = available
        super().__init__(message or f"ChatGPT 当前页面没有可用的选择：{requested}")


@dataclass
class SubmitReceipt:
    conversation_url: str
    assistant_count_before: int
    selected_model: str | None = None
    requested_quality: str | None = None
    requested_reasoning_effort: str | None = None
    resolved_model: str | None = None
    resolved_reasoning_effort: str | None = None
    selection_source: str | None = None
    selection_verified_at: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BrowserCapabilities:
    models: list[str]
    current_model: str | None
    reasoning_efforts: list[dict[str, str | int]]
    current_reasoning_effort: dict[str, str | int] | None
    quality_aliases: dict[str, str | None]
    url: str
    logged_in: bool


@dataclass
class BrowserSnapshot:
    state: str
    conversation_url: str | None
    progress: str | None = None
    user_action: str | None = None
    result_text: str | None = None
    sources: list[str] = field(default_factory=list)
    page_title: str | None = None


@dataclass
class BrowserExport:
    """Raw conversation data extracted from the visible logged-in page."""

    conversation_url: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)


def classify_page_text(
    text: str,
    *,
    has_new_output: bool,
    has_research_output: bool = False,
    active_hint: bool | None = None,
) -> tuple[str, str | None]:
    lower = text.lower()
    blocked = (
        ("captcha" in lower and not has_new_output)
        or ("verify you are human" in lower)
        or ("验证你是人类" in lower)
        or ("人机验证" in lower)
    )
    if blocked:
        return "NEEDS_USER_INPUT", "ChatGPT 页面要求完成人机验证，请在专用浏览器窗口中手动完成。"
    auth = (
        "auth.openai.com" in lower
        or "log in to chatgpt" in lower
        or "sign in to chatgpt" in lower
        or "登录 chatgpt" in lower
    )
    if auth and not has_new_output:
        return "NEEDS_USER_INPUT", "ChatGPT 尚未登录或登录已过期，请在专用浏览器 Profile 中手动登录。"
    clarification = any(
        phrase in lower
        for phrase in (
            "needs your input",
            "需要你的输入",
            "请确认研究计划",
            "please clarify",
            "请提供更多信息",
        )
    )
    if clarification:
        return "NEEDS_USER_INPUT", "ChatGPT 正在等待用户澄清或确认，请检查专用浏览器页面。"
    active = active_hint if active_hint is not None else any(
        phrase in lower
        for phrase in ("stop generating", "正在生成", "正在思考", "正在搜索", "正在研究")
    )
    if active or (not has_new_output and not has_research_output):
        return "RUNNING", None
    return "COMPLETE", None


def is_transient_assistant_text(text: str | None) -> bool:
    if not text:
        return False
    return bool(
        re.fullmatch(
            r"\s*(?:(?:pro|gpt[-\w]+)\s*)?(?:thinking|思考中|正在思考|正在生成|generating)(?:[.…]*)\s*",
            text,
            re.I,
        )
    )


class ChatGPTBrowser:
    _QUALITY_ALIASES = {
        "extra_high": {"extra high", "extra_high", "extra-high", "extrahigh", "极高", "最高"},
        "pro": {"pro", "专业", "专业级"},
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._connected_cdp = False
        self._launched_cdp_process = None

    async def start(self) -> None:
        if self._context:
            return
        self.settings.prepare_dirs()
        self._playwright = await async_playwright().start()
        if self.settings.browser_cdp_url:
            try:
                await self._connect_cdp()
                return
            except Exception:
                await self._reset_connection()
                if self.settings.browser_launch_cdp:
                    self._launch_standard_chrome()
                    for _ in range(30):
                        try:
                            await self._connect_cdp()
                            return
                        except Exception:
                            await self._reset_connection()
                            await asyncio.sleep(0.5)
                # Fall through to a persistent context only when a CDP endpoint cannot be used.
        executable = self.settings.chrome_executable or default_chrome_executable()
        launch_args = ["--disable-backgrounding-occluded-windows"]
        if self.settings.browser_start_minimized and not self.settings.browser_headless:
            launch_args.append("--start-minimized")
        kwargs: dict[str, Any] = {
            "headless": self.settings.browser_headless,
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 1000},
            "args": launch_args,
        }
        if executable and executable.exists():
            kwargs["executable_path"] = str(executable)
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.settings.browser_profile_dir),
                **kwargs,
            )
        except Exception:
            await self.close()
            raise

    async def _connect_cdp(self) -> None:
        assert self._playwright is not None
        self._browser = await self._playwright.chromium.connect_over_cdp(self.settings.browser_cdp_url)
        contexts = self._browser.contexts
        if not contexts:
            raise BrowserTaskError("CDP 浏览器没有可用的浏览器上下文。")
        self._context = contexts[0]
        self._connected_cdp = True

    def _launch_standard_chrome(self) -> None:
        executable = self.settings.chrome_executable or default_chrome_executable()
        if not executable or not executable.exists():
            raise BrowserTaskError("找不到正式 Chrome，可设置 CHATGPT_CHROME_EXECUTABLE。")
        parsed = urlparse(self.settings.browser_cdp_url or "http://127.0.0.1:9222")
        port = parsed.port or 9222
        args = [
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.settings.browser_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        args.append("--start-minimized" if self.settings.browser_start_minimized else "--start-maximized")
        args.append("https://chatgpt.com/")
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._launched_cdp_process = subprocess.Popen(
            [str(executable), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    async def _reset_connection(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._connected_cdp = False

    async def close(self) -> None:
        if self._connected_cdp:
            await self._reset_connection()
        elif self._context:
            try:
                await self._context.close()
            finally:
                self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _page(self, *, navigate: bool = True) -> Page:
        await self.start()
        assert self._context is not None
        for page in self._context.pages:
            if "chatgpt.com" in page.url or "chat.openai.com" in page.url:
                return page
        page = await self._context.new_page()
        if navigate:
            try:
                await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=int(self.settings.browser_timeout_seconds * 1000))
            except PlaywrightError:
                # A partially loaded ChatGPT SPA is still inspectable and may only need a manual login.
                pass
        return page

    async def _conversation_page(self, conversation_url: str, *, create: bool = True) -> Page:
        """Return the exact ChatGPT tab for a conversation, never another session's tab."""
        await self.start()
        assert self._context is not None
        target = conversation_url.rstrip("/")
        for page in self._context.pages:
            if page.url.rstrip("/") == target:
                try:
                    await page.bring_to_front()
                except PlaywrightError:
                    pass
                return page
        if not create:
            raise BrowserTaskError(f"ChatGPT conversation tab is not open: {conversation_url}")
        page = await self._context.new_page()
        try:
            await page.goto(
                conversation_url,
                wait_until="domcontentloaded",
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        except PlaywrightError:
            pass
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass
        return page

    async def _fresh_conversation_page(self) -> Page:
        await self.start()
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(
                "https://chatgpt.com/",
                wait_until="domcontentloaded",
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        except PlaywrightError:
            pass
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass
        return page

    async def has_conversation_tab(self, conversation_url: str) -> bool:
        if self._context is None:
            return False
        target = conversation_url.rstrip("/")
        return any(page.url.rstrip("/") == target for page in self._context.pages)

    async def close_conversation_tabs(self, conversation_url: str) -> bool:
        if self._context is None:
            return False
        target = conversation_url.rstrip("/")
        closed = False
        for page in list(self._context.pages):
            if page.url.rstrip("/") != target:
                continue
            try:
                await page.close()
                closed = True
            except PlaywrightError:
                continue
        return closed

    async def browser_status(self) -> dict[str, Any]:
        page = await self._page()
        text = await self._page_text(page)
        logged_in = await self._page_has_composer(page)
        return {
            "ready": logged_in,
            "logged_in": logged_in,
            "url": page.url,
            "title": await page.title(),
            "profile_dir": str(self.settings.browser_profile_dir),
            "headless": self.settings.browser_headless,
            "cdp_url": self.settings.browser_cdp_url,
            "message": None if logged_in else "请在专用浏览器 Profile 中完成 ChatGPT 登录。",
        }

    async def _page_has_composer(self, page: Page) -> bool:
        if "chatgpt.com" not in page.url and "chat.openai.com" not in page.url:
            return False
        if "auth.openai.com" in page.url:
            return False
        composer_candidates = page.locator("textarea, [contenteditable='true']")
        for index in range(await composer_candidates.count()):
            if await composer_candidates.nth(index).is_visible():
                return True
        return False

    async def _page_requires_login(self, page: Page) -> bool:
        if _is_explicit_login_page(page.url):
            return True
        login_fields = page.locator("input[type='email']:visible, input[name='username']:visible")
        return await login_fields.count() > 0

    async def _intelligence_trigger(self, page: Page) -> Locator:
        candidates = page.locator("[data-composer-body] button.__composer-pill")
        if await candidates.count():
            return candidates.last
        candidates = page.locator("[data-composer-body] button[aria-haspopup='menu']")
        if await candidates.count():
            return candidates.last
        raise BrowserTaskError("找不到 ChatGPT 模型/能力选择器。")

    async def _open_intelligence_menu(self, page: Page) -> Locator:
        menu = page.locator('[data-testid="composer-intelligence-picker-content"]')
        for attempt in range(2):
            await self._close_intelligence_menu(page)
            try:
                trigger = await self._intelligence_trigger(page)
                await trigger.wait_for(state="visible", timeout=5000)
                if not await trigger.is_enabled():
                    raise BrowserTaskError("ChatGPT 模型/能力选择器当前不可用。")
                await trigger.click()
                await menu.wait_for(state="visible", timeout=5000)
                slider = menu.locator('[data-model-reasoning-effort-slider] [role="slider"]')
                for _ in range(20):
                    try:
                        if await slider.is_visible():
                            await page.wait_for_timeout(250)
                            return menu
                    except PlaywrightError:
                        pass
                    await asyncio.sleep(0.1)
            except PlaywrightError:
                if attempt == 0:
                    await page.wait_for_timeout(500)
                    continue
                raise BrowserTaskError("ChatGPT 模型/能力菜单没有打开。")
        raise BrowserTaskError("ChatGPT 模型/能力菜单没有打开。")

    async def _close_intelligence_menu(self, page: Page) -> None:
        menu = page.locator('[data-testid="composer-intelligence-picker-content"]')
        for _ in range(3):
            try:
                if not await menu.is_visible():
                    return
            except PlaywrightError:
                return
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(250)

    @staticmethod
    def _normalize_label(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().casefold())

    async def _effort_control(self, menu: Locator) -> Locator:
        control = menu.get_by_role("menuitem", name=re.compile(r"能力|reasoning|effort", re.I))
        if await control.count():
            return control.first
        raise BrowserTaskError("当前 ChatGPT 页面没有找到能力滑块。")

    async def _effort_label(self, menu: Locator) -> str:
        lines = [line.strip() for line in (await menu.inner_text()).splitlines() if line.strip()]
        for line in lines:
            match = re.match(r"(.+?)[,，]\s*第\s*\d+\s*项", line)
            if match:
                return match.group(1).strip()
        return lines[0] if lines else ""

    async def _press_effort_key(self, menu: Locator, key: str) -> None:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                control = await self._effort_control(menu)
                await control.focus()
                await control.press(key, timeout=3000)
                await asyncio.sleep(0.05)
                return
            except PlaywrightError as exc:
                last_error = exc
                await asyncio.sleep(0.05)
        raise BrowserTaskError(f"能力滑块无法响应 {key}") from last_error

    async def _discover_efforts(self, menu: Locator, *, restore: bool = True) -> tuple[list[dict[str, str | int]], int]:
        slider = menu.locator('[data-model-reasoning-effort-slider] [role="slider"]')
        maximum = int(await slider.get_attribute("aria-valuemax") or "4")
        original = int(await slider.get_attribute("aria-valuenow") or "0")
        efforts: list[dict[str, str | int]] = []
        # This custom Radix slider responds reliably to arrow keys; Home is
        # ignored when the current value is already at the maximum.
        for _ in range(maximum + 1):
            if int(await slider.get_attribute("aria-valuenow") or "0") <= 0:
                break
            await self._press_effort_key(menu, "ArrowLeft")
        for value in range(maximum + 1):
            if value:
                await self._press_effort_key(menu, "ArrowRight")
                await asyncio.sleep(0.03)
            current = int(await slider.get_attribute("aria-valuenow") or str(value))
            efforts.append({"value": current, "label": await self._effort_label(menu)})
        if restore:
            for _ in range(maximum + 1):
                if int(await slider.get_attribute("aria-valuenow") or "0") <= 0:
                    break
                await self._press_effort_key(menu, "ArrowLeft")
            for _ in range(original):
                await self._press_effort_key(menu, "ArrowRight")
            await asyncio.sleep(0.1)
        return efforts, original

    async def _model_view(self, menu: Locator) -> Locator:
        radios = menu.locator('[role="menuitemradio"]')
        visible_count = 0
        for index in range(await radios.count()):
            if await radios.nth(index).is_visible():
                visible_count += 1
        if visible_count == 0:
            toggle = menu.get_by_role("menuitem", name=re.compile(r"选择模型|select model", re.I))
            if await toggle.count():
                await toggle.click()
                await asyncio.sleep(0.1)
        return menu.locator('[role="menuitemradio"]')

    async def _read_model_options(self, menu: Locator) -> tuple[list[str], str | None]:
        radios = await self._model_view(menu)
        models: list[str] = []
        current: str | None = None
        for index in range(await radios.count()):
            radio = radios.nth(index)
            text = " ".join(line.strip() for line in (await radio.inner_text()).splitlines() if line.strip())
            if not text:
                continue
            models.append(text)
            if await radio.get_attribute("aria-checked") == "true":
                current = text
        return models, current

    async def capabilities(self) -> BrowserCapabilities:
        page = await self._page()
        status = await self.browser_status()
        menu = await self._open_intelligence_menu(page)
        efforts, original = await self._discover_efforts(menu)
        models, current_model = await self._read_model_options(menu)
        aliases: dict[str, str | None] = {"extra_high": None, "pro": None}
        for item in efforts:
            label = str(item["label"])
            normalized = self._normalize_label(label)
            for quality, names in self._QUALITY_ALIASES.items():
                if normalized in names or any(name in normalized for name in names):
                    aliases[quality] = label
        await self._close_intelligence_menu(page)
        current_effort = next((item for item in efforts if int(item["value"]) == original), None)
        return BrowserCapabilities(
            models=models,
            current_model=current_model,
            reasoning_efforts=efforts,
            current_reasoning_effort=current_effort,
            quality_aliases=aliases,
            url=page.url,
            logged_in=bool(status["logged_in"]),
        )

    async def _select_effort(
        self,
        page: Page,
        target: str,
        *,
        allow_downgrade: bool = False,
    ) -> tuple[str, int, str]:
        menu = await self._open_intelligence_menu(page)
        efforts, _ = await self._discover_efforts(menu)
        target_normalized = self._normalize_label(target)

        def matches(label: str) -> bool:
            normalized = self._normalize_label(label)
            aliases = self._QUALITY_ALIASES.get(target_normalized, {target_normalized})
            return normalized in aliases or any(alias in normalized for alias in aliases)

        selected = next((item for item in efforts if matches(str(item["label"]))), None)
        source = "explicit"
        if selected is None and target_normalized == "pro" and allow_downgrade:
            selected = next(
                (item for item in efforts if self._normalize_label(str(item["label"])) in self._QUALITY_ALIASES["extra_high"]),
                None,
            )
            source = "downgrade"
        if selected is None:
            await self._close_intelligence_menu(page)
            raise ModelUnavailableError(target, [str(item["label"]) for item in efforts])
        control = await self._effort_control(menu)
        slider = menu.locator('[data-model-reasoning-effort-slider] [role="slider"]')
        maximum = int(await slider.get_attribute("aria-valuemax") or "4")
        for _ in range(maximum + 1):
            if int(await slider.get_attribute("aria-valuenow") or "0") <= 0:
                break
            await self._press_effort_key(menu, "ArrowLeft")
        for _ in range(int(selected["value"])):
            await self._press_effort_key(menu, "ArrowRight")
        actual = await self._effort_label(menu)
        if not matches(actual):
            await self._close_intelligence_menu(page)
            raise BrowserTaskError(f"能力选择未验证：请求 {target}，页面显示 {actual}")
        await self._close_intelligence_menu(page)
        return actual, int(selected["value"]), source

    async def _select_model_option(self, page: Page, model: str) -> str:
        menu = await self._open_intelligence_menu(page)
        radios = await self._model_view(menu)
        target = self._normalize_label(model)
        for index in range(await radios.count()):
            radio = radios.nth(index)
            text = " ".join(line.strip() for line in (await radio.inner_text()).splitlines() if line.strip())
            if target == self._normalize_label(text) or target in self._normalize_label(text):
                await radio.click()
                await self._close_intelligence_menu(page)
                return text
        available = [
            " ".join(line.strip() for line in (await radios.nth(index).inner_text()).splitlines() if line.strip())
            for index in range(await radios.count())
        ]
        await self._close_intelligence_menu(page)
        raise ModelUnavailableError(model, [item for item in available if item])

    async def _apply_selection(
        self,
        page: Page,
        *,
        quality: str,
        model: str | None,
        reasoning_effort: str | None,
        allow_downgrade: bool,
    ) -> dict[str, str | None]:
        resolved_model = None
        if model and self._normalize_label(model) not in {"pro", "extra_high", "extra high", "极高"}:
            resolved_model = await self._select_model_option(page, model)
        target_effort = reasoning_effort or (quality if quality != "auto" else None)
        if not target_effort and model and self._normalize_label(model) in self._QUALITY_ALIASES:
            target_effort = self._normalize_label(model)
        resolved_effort = None
        source = "explicit_model" if resolved_model else "none"
        if target_effort:
            resolved_effort, _, effort_source = await self._select_effort(
                page, target_effort, allow_downgrade=allow_downgrade
            )
            source = effort_source if not resolved_model else f"{source}+{effort_source}"
        return {
            "resolved_model": resolved_model,
            "resolved_reasoning_effort": resolved_effort,
            "selection_source": source,
            "selection_verified_at": utc_now(),
        }

    async def submit(
        self,
        mode: str,
        prompt: str,
        *,
        model: str | None = None,
        quality: str = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        attachments: list[InMemoryUpload] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        dedicated_page: bool = False,
    ) -> SubmitReceipt:
        page = await (self._fresh_conversation_page() if dedicated_page else self._page())
        status = await self.browser_status()
        if not status["logged_in"] and not await self._page_has_composer(page):
            raise BrowserNeedsUserAction(
                "login_required",
                "请在专用浏览器 Profile 中登录 ChatGPT，完成后再次查询任务或重新提交。",
            )
        # Every new task gets a fresh ChatGPT conversation. Continuations use
        # continue_conversation() and deliberately keep the original URL.
        if page.url.rstrip("/") != "https://chatgpt.com":
            await page.goto(
                "https://chatgpt.com/",
                wait_until="domcontentloaded",
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        selection = await self._apply_selection(
            page,
            quality=quality,
            model=model,
            reasoning_effort=reasoning_effort,
            allow_downgrade=allow_downgrade,
        )
        if mode == "research":
            await self._activate_research(page)
        attachment_receipt: list[dict[str, Any]] = []
        try:
            attachment_receipt = await self._upload_files(page, attachments or [])
            if attachment_receipt and on_upload_verified:
                await on_upload_verified(attachment_receipt)
            composer = await self._find_composer(page)
            before = await self._assistant_count(page)
            await composer.fill(prompt)
            await self._click_send(page)
        except Exception:
            await self._clear_uploaded_files(page, [item.metadata.filename for item in (attachments or [])])
            raise
        await page.wait_for_timeout(750)
        if mode == "research":
            await self._start_research_plan_if_present(page)
        return SubmitReceipt(
            conversation_url=page.url,
            assistant_count_before=before,
            selected_model=model,
            requested_quality=quality,
            requested_reasoning_effort=reasoning_effort,
            resolved_model=selection["resolved_model"],
            resolved_reasoning_effort=selection["resolved_reasoning_effort"],
            selection_source=selection["selection_source"],
            selection_verified_at=selection["selection_verified_at"],
            attachments=attachment_receipt,
        )

    async def continue_conversation(
        self,
        conversation_url: str,
        prompt: str,
        *,
        attachments: list[InMemoryUpload] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    ) -> SubmitReceipt:
        page = await self._conversation_page(conversation_url)
        status = await self.browser_status()
        if not status["logged_in"] and not await self._page_has_composer(page):
            raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中重新登录 ChatGPT。")
        attachment_receipt: list[dict[str, Any]] = []
        try:
            attachment_receipt = await self._upload_files(page, attachments or [])
            if attachment_receipt and on_upload_verified:
                await on_upload_verified(attachment_receipt)
            before = await self._assistant_count(page)
            composer = await self._find_composer(page)
            await composer.fill(prompt)
            await self._click_send(page)
        except Exception:
            await self._clear_uploaded_files(page, [item.metadata.filename for item in (attachments or [])])
            raise
        await page.wait_for_timeout(750)
        return SubmitReceipt(page.url, before, attachments=attachment_receipt)

    @staticmethod
    def _combine_api_pages(
        latest_payload: dict[str, Any],
        older_pages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        page_records = [
            *reversed(older_pages),
            {"payload": latest_payload, "cursor_matches": True},
        ]
        messages: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        duplicate_count = 0
        conflicting_duplicate = False
        raw_message_count = 0
        page_flags: list[dict[str, bool | None]] = []
        cursor_mismatches = 0
        for record in page_records:
            payload = record.get("payload")
            if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                continue
            raw_message_count += len(payload["messages"])
            info = payload.get("page_info") if isinstance(payload.get("page_info"), dict) else {}
            previous = info.get("has_previous_page")
            next_page = info.get("has_next_page")
            page_flags.append({
                "has_previous_page": previous if isinstance(previous, bool) else None,
                "has_next_page": next_page if isinstance(next_page, bool) else None,
            })
            if record.get("cursor_matches") is False:
                cursor_mismatches += 1
            for message in payload["messages"]:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if not isinstance(message_id, str) or not message_id:
                    messages.append(message)
                    continue
                previous_message = by_id.get(message_id)
                if previous_message is not None:
                    duplicate_count += 1
                    if json.dumps(previous_message, sort_keys=True, ensure_ascii=False, default=str) != json.dumps(
                        message, sort_keys=True, ensure_ascii=False, default=str
                    ):
                        conflicting_duplicate = True
                    continue
                by_id[message_id] = message
                messages.append(message)

        if not page_flags:
            state = "unverified"
            reason = "api_page_info_missing"
        else:
            oldest_reached = page_flags[0]["has_previous_page"] is False
            newest_reached = page_flags[-1]["has_next_page"] is False
            known_more = any(
                flag["has_previous_page"] is True or flag["has_next_page"] is True
                for flag in (page_flags[0], page_flags[-1])
            )
            if cursor_mismatches or conflicting_duplicate:
                state = "partial"
                reason = "api_page_chain_inconsistent"
            elif oldest_reached and newest_reached:
                state = "verified"
                reason = "api_page_boundaries_reached"
            elif known_more:
                state = "partial"
                reason = "api_pages_incomplete"
            else:
                state = "unverified"
                reason = "api_page_boundaries_unknown"
        return {
            "messages": messages,
            "pagination": {
                "page_count": len(page_records),
                "raw_message_count": raw_message_count,
                "page_flags": page_flags,
                "cursor_mismatch_count": cursor_mismatches,
                "duplicate_count": duplicate_count,
                "conflicting_duplicate": conflicting_duplicate,
                "completeness": state,
                "reason": reason,
            },
        }

    async def export_conversation(
        self,
        conversation_url: str,
        *,
        scope: str = "recent",
        count: int = 10,
        stream_pages: bool = False,
    ) -> dict[str, Any]:
        """Capture the logged-in page's own conversation and pagination responses."""
        parsed = urlparse(conversation_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"}:
            raise BrowserTaskError("conversation_url must point to chatgpt.com")
        await self.start()
        assert self._context is not None
        conversation_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        page = await self._context.new_page()
        message_spool: PagedMessageSpool | None = None
        spool_transferred = False
        api_attempts: list[dict[str, Any]] = []
        page_events: list[dict[str, Any]] = []
        file_download_responses: list[dict[str, Any]] = []
        file_download_response_urls: set[str] = set()
        file_download_response_file_ids: set[str] = set()
        interpreter_response_urls: set[str] = set()
        interpreter_attachments: list[dict[str, Any]] = []
        interpreter_attachment_map: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        interpreter_failures = 0
        api_attachment_metadata_attempt_count = 0
        api_attachment_metadata_resolved_count = 0
        api_attachment_metadata_failure_count = 0
        pending: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        attachment_activity = {"response_count": 0}

        def record_interpreter_metadata(
            source_url: str,
            data: Any,
            attempt: dict[str, Any],
        ) -> None:
            nonlocal interpreter_failures
            if not isinstance(data, dict):
                interpreter_failures += 1
                attempt["error_type"] = "unexpected_payload_shape"
                return
            source = urlparse(source_url)
            query = parse_qs(source.query)
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            file_id = metadata.get("file_id")
            message_id = (query.get("message_id") or [None])[0]
            sandbox_path = (query.get("sandbox_path") or [None])[0]
            file_name = data.get("file_name")
            if not _interpreter_metadata_resource_urls([source_url], conversation_url, conversation_id):
                interpreter_failures += 1
                attempt["error_type"] = "unverified_interpreter_metadata_route"
                return
            expected_name = unquote(str(sandbox_path or "").rsplit("/", 1)[-1])
            if not isinstance(file_name, str) or not file_name:
                interpreter_failures += 1
                attempt["error_type"] = "file_metadata_missing"
                return
            if expected_name and file_name.lower() != expected_name.lower():
                interpreter_failures += 1
                attempt["error_type"] = "interpreter_filename_mismatch"
                return
            download_url = data.get("download_url")
            download_available = data.get("status") == "success" and isinstance(download_url, str) and bool(download_url)
            if download_available:
                final_url = urljoin(source_url, str(download_url))
                final = urlparse(final_url)
                signed_file_id = (parse_qs(final.query).get("id") or [None])[0]
                if (
                    final.scheme != "https"
                    or not _is_chatgpt_download_host(final.hostname or "")
                    or final.path != "/backend-api/estuary/content"
                    or file_id and signed_file_id != str(file_id)
                ):
                    interpreter_failures += 1
                    attempt["error_type"] = "interpreter_content_route_unverified"
                    return
            if not file_id and not file_name:
                interpreter_failures += 1
                attempt["error_type"] = "file_metadata_missing"
                return
            fallback_identity = f"{conversation_id}\0{message_id or ''}\0{sandbox_path or ''}"
            attachment_id = str(
                file_id
                or f"interpreter-{uuid.uuid5(uuid.NAMESPACE_URL, fallback_identity).hex[:16]}"
            )
            dedupe_key = (
                str(message_id or ""),
                str(sandbox_path or ""),
                str(file_id or ""),
                str(file_name or ""),
            )
            attachment = interpreter_attachment_map.get(dedupe_key)
            if attachment is None:
                attachment = {
                    "attachment_id": attachment_id,
                    "id": attachment_id,
                    "file_id": str(file_id) if file_id else None,
                    "message_id": str(message_id) if message_id else None,
                    "filename": str(file_name or "attachment"),
                    "mime_type": data.get("mime_type"),
                    "size": data.get("file_size_bytes"),
                    "source_url": source_url,
                    "file_kind": "interpreter_output",
                    "downloadable": download_available,
                    "unavailable_reason": None if download_available else "ChatGPT did not expose file download metadata",
                    "sandbox_path": sandbox_path,
                }
                interpreter_attachment_map[dedupe_key] = attachment
                interpreter_attachments.append(attachment)
                if not download_available:
                    interpreter_failures += 1
                else:
                    attempt["metadata_status"] = "success"
            elif download_available and not attachment.get("downloadable"):
                attachment.update(
                    {
                        "source_url": source_url,
                        "mime_type": data.get("mime_type") or attachment.get("mime_type"),
                        "size": data.get("file_size_bytes") or attachment.get("size"),
                        "downloadable": True,
                        "unavailable_reason": None,
                    }
                )
                interpreter_failures = max(0, interpreter_failures - 1)
                attempt["metadata_status"] = "success"
            elif download_available:
                attempt["metadata_status"] = "success"
            else:
                attempt["error_type"] = "file_metadata_unavailable"

        def record_file_download_metadata(
            source_url: str,
            status: int,
            data: Any,
            attempt: dict[str, Any],
        ) -> None:
            if status != 200:
                attempt["error_type"] = "http_error"
                return
            if not isinstance(data, dict):
                attempt["error_type"] = "unexpected_payload_shape"
                return
            source = urlparse(source_url)
            route = "/backend-api/files/download/"
            file_id = unquote(source.path[len(route):].split("/", 1)[0])
            if not _is_observed_file_download_url(source_url, file_id):
                attempt["error_type"] = "unverified_file_route_shape"
                return
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            actual_file_id = metadata.get("file_id")
            if actual_file_id and str(actual_file_id) != file_id:
                attempt["error_type"] = "file_id_mismatch"
                return
            download_url = data.get("download_url")
            final_url = urljoin(source_url, download_url) if isinstance(download_url, str) else ""
            final = urlparse(final_url)
            if (
                data.get("status") != "success"
                or not download_url
                or final.scheme != "https"
                or not _is_chatgpt_download_host(final.hostname or "")
                or not ("/estuary/content" in final.path or "/files/download/" in final.path)
            ):
                attempt["error_type"] = "file_metadata_unavailable"
                return
            if final.path == "/backend-api/estuary/content" and (
                (parse_qs(final.query).get("id") or [None])[0] != file_id
            ):
                attempt["error_type"] = "content_id_mismatch"
                return
            file_download_responses.append(
                {
                    "file_id": file_id,
                    "source_url": source_url,
                    "status": "success",
                    "has_download_url": True,
                    "filename": data.get("file_name"),
                    "mime_type": data.get("mime_type") or metadata.get("mime_type"),
                    "size": data.get("file_size_bytes") or metadata.get("file_size_bytes"),
                }
            )
            attempt["metadata_status"] = "success"
            attempt["download_url_present"] = True

        async def capture_response(response: Any) -> None:
            nonlocal interpreter_failures
            response_url = urlparse(str(response.url))
            route = response_url.path
            query = parse_qs(response_url.query)
            base_path = f"/backend-api/conversations/{conversation_id}"
            page_path = f"{base_path}/messages"
            interpreter_path = f"/backend-api/conversation/{conversation_id}/interpreter/download"

            if route == base_path or route == page_path:
                direction = (
                    "older" if "before" in query else
                    "newer" if "after" in query else
                    "latest" if route == base_path else
                    "unknown"
                )
                if route == page_path and direction in {"older", "newer"}:
                    return
                attempt: dict[str, Any] = {
                    "route": "conversation" if route == base_path else "conversation_messages",
                    "status": response.status,
                    "direction": direction,
                    "query_keys": sorted(query.keys()),
                }
                api_attempts.append(attempt)
                if not response.ok:
                    return
                try:
                    payload = await response.json()
                except (PlaywrightError, ValueError):
                    attempt["error_type"] = "invalid_json"
                    return
                if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                    attempt["error_type"] = "unexpected_payload_shape"
                    return
                page_info = payload.get("page_info") if isinstance(payload.get("page_info"), dict) else {}
                request_cursor = (query.get("before") or query.get("after") or [None])[0]
                event = {
                    "direction": direction,
                    "request_cursor": request_cursor,
                    "payload": payload,
                }
                if direction == "latest":
                    try:
                        request_headers = await response.request.all_headers()
                    except PlaywrightError:
                        request_headers = {}
                    event["request_url"] = str(response.url)
                    event["request_headers"] = {
                        name.lower(): value
                        for name, value in request_headers.items()
                        if name.lower() == "authorization"
                        or name.lower().startswith(("oai-", "x-oai-", "x-openai-"))
                    }
                page_events.append(event)
                attempt["message_count"] = len(payload["messages"])
                attempt["has_previous_page"] = page_info.get("has_previous_page")
                attempt["has_next_page"] = page_info.get("has_next_page")
                return

            file_download_path = "/backend-api/files/download/"
            request_url = str(response.request.url)
            request = urlparse(request_url)
            request_query = parse_qs(request.query)
            if request.path.startswith(file_download_path):
                if request_url in file_download_response_urls:
                    return
                file_download_response_urls.add(request_url)
                file_id = unquote(request.path[len(file_download_path):].split("/", 1)[0])
                file_download_response_file_ids.add(file_id)
                attempt = {
                    "route": "file_download_metadata",
                    "status": response.status,
                    "query_keys": sorted(request_query.keys()),
                }
                api_attempts.append(attempt)
                try:
                    data = await response.json()
                except (PlaywrightError, ValueError):
                    data = None
                    attempt["error_type"] = "invalid_json"
                record_file_download_metadata(request_url, response.status, data, attempt)
                return

            if route != interpreter_path:
                return
            source_url = str(response.url)
            attachment_activity["response_count"] += 1
            interpreter_response_urls.add(source_url)
            attempt = {
                "route": "interpreter_download_metadata",
                "status": response.status,
                "query_keys": sorted(query.keys()),
            }
            api_attempts.append(attempt)
            if not _interpreter_metadata_resource_urls([source_url], conversation_url, conversation_id):
                interpreter_failures += 1
                attempt["error_type"] = "unverified_interpreter_metadata_route"
                return
            if not response.ok:
                interpreter_failures += 1
                attempt["error_type"] = "http_error"
                return
            try:
                data = await response.json()
            except (PlaywrightError, ValueError):
                interpreter_failures += 1
                attempt["error_type"] = "invalid_json"
                return
            record_interpreter_metadata(source_url, data, attempt)

        def on_response(response: Any) -> None:
            task = asyncio.create_task(capture_response(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        page.on("response", on_response)
        try:
            try:
                await page.goto(
                    conversation_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.settings.browser_timeout_seconds * 1000),
                )
            except PlaywrightError:
                pass
            logged_in = False
            explicit_login_page = False
            ready_deadline = loop.time() + max(3.0, self.settings.browser_timeout_seconds)
            while loop.time() < ready_deadline:
                try:
                    if await self._page_has_composer(page):
                        logged_in = True
                        break
                except PlaywrightError:
                    pass
                if _has_authenticated_conversation_response(page_events):
                    logged_in = True
                    break
                try:
                    explicit_login_page = await self._page_requires_login(page)
                except PlaywrightError:
                    explicit_login_page = False
                if explicit_login_page:
                    break
                await page.wait_for_timeout(250)
            if not logged_in:
                if explicit_login_page:
                    raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中重新登录 ChatGPT。")
                raise BrowserTaskError("ChatGPT conversation page did not become ready for export")

            latest_event: dict[str, Any] | None = None
            for _ in range(100):
                latest_event = next((event for event in page_events if event["direction"] == "latest"), None)
                if latest_event:
                    break
                await page.wait_for_timeout(100)

            if latest_event is None or not latest_event["payload"].get("messages"):
                load_result = await self._load_all_rendered_messages(page)
                extracted = await self._extract_dom_conversation(page, conversation_url)
                messages = extracted.get("messages", [])
                message_state = "partial" if not load_result.get("stable") else "unverified"
                result = {
                    **extracted,
                    "source": "dom",
                    "completeness": {
                        "messages": message_state,
                        "attachments": "unverified",
                        "overall": message_state,
                    },
                    "reason": {
                        "messages": "api_response_unavailable" if latest_event is None else "api_message_list_empty",
                        "attachments": "dom_attachment_terminal_unavailable",
                    },
                    "provenance": {
                        "api_attempts": api_attempts,
                        "api_page_count": 0,
                        "rendered_message_count": len(messages),
                        "dom_load": load_result,
                    },
                }
                if not messages:
                    result["error_code"] = "NO_MESSAGES_READ"
                if interpreter_attachments:
                    result["attachments"] = interpreter_attachments
                return result

            await page.wait_for_timeout(750)
            if pending:
                await asyncio.gather(*tuple(pending), return_exceptions=True)
            page.remove_listener("response", on_response)

            async def fetch_older_page(current_payload: dict[str, Any]) -> dict[str, Any] | None:
                info = current_payload.get("page_info") if isinstance(current_payload.get("page_info"), dict) else {}
                expected_cursor = info.get("start_cursor")
                if not isinstance(expected_cursor, str) or not expected_cursor:
                    api_attempts.append(
                        {
                            "route": "conversation_messages",
                            "direction": "older",
                            "error_type": "page_cursor_missing",
                        }
                    )
                    return None
                current_url = urlparse(page.url)
                if (
                    current_url.hostname not in {
                        "chatgpt.com",
                        "www.chatgpt.com",
                        "chat.openai.com",
                        "www.chat.openai.com",
                    }
                    or current_url.path.rstrip("/") != parsed.path.rstrip("/")
                ):
                    api_attempts.append(
                        {
                            "route": "conversation_messages",
                            "direction": "older",
                            "error_type": "conversation_url_changed",
                        }
                    )
                    return None

                latest_request_url = latest_event.get("request_url")
                request_headers = latest_event.get("request_headers")
                if not isinstance(latest_request_url, str) or not isinstance(request_headers, dict):
                    return None
                try:
                    request_url, client_headers = _conversation_messages_request(
                        conversation_url,
                        conversation_id,
                        latest_request_url,
                        expected_cursor,
                        request_headers,
                    )
                except ValueError:
                    api_attempts.append(
                        {
                            "route": "conversation_messages",
                            "direction": "older",
                            "error_type": "request_context_unavailable",
                        }
                    )
                    return None

                cursor_matches = (
                    (parse_qs(urlparse(request_url).query).get("before") or [None])[0]
                    == expected_cursor
                )
                request_result = await _authenticated_page_json_get(
                    page,
                    request_url,
                    client_headers,
                )
                query_keys = sorted(parse_qs(urlparse(request_url).query))
                for item in request_result["attempts"]:
                    api_attempts.append(
                        {
                            "route": "conversation_messages",
                            "direction": "older",
                            "query_keys": query_keys,
                            **item,
                        }
                    )
                payload = request_result.get("payload")
                if (
                    request_result.get("status") != 200
                    or not isinstance(payload, dict)
                    or not isinstance(payload.get("messages"), list)
                ):
                    if request_result.get("status") == 200 and request_result.get("attempts"):
                        api_attempts[-1]["error_type"] = "unexpected_payload_shape"
                    return None

                page_info = payload.get("page_info") if isinstance(payload.get("page_info"), dict) else {}
                attempt = api_attempts[-1]
                attempt["message_count"] = len(payload["messages"])
                attempt["has_previous_page"] = page_info.get("has_previous_page")
                attempt["has_next_page"] = page_info.get("has_next_page")
                return {
                    "direction": "older",
                    "request_cursor": expected_cursor,
                    "cursor_matches": cursor_matches,
                    "payload": payload,
                }

            latest_payload = latest_event["payload"]
            if stream_pages:
                message_spool = PagedMessageSpool(
                    self.settings.jobs_dir / "export-snapshots",
                    max_bytes=self.settings.export_max_snapshot_bytes,
                )
                latest_source_bytes = message_spool.measure_page(latest_payload)
                latest_normalized = self._normalise_api_conversation(latest_payload, conversation_url)
                message_spool.append_page(
                    latest_payload,
                    latest_normalized,
                    cursor_matches=True,
                    source_page_bytes=latest_source_bytes,
                )
                del latest_normalized

                async def stage_older_page(page_record: dict[str, Any]) -> None:
                    page_payload = page_record["payload"]
                    source_page_bytes = message_spool.measure_page(page_payload)
                    normalized_page = self._normalise_api_conversation(page_payload, conversation_url)
                    message_spool.append_page(
                        page_payload,
                        normalized_page,
                        cursor_matches=page_record.get("cursor_matches") is True,
                        source_page_bytes=source_page_bytes,
                    )

                older_pages, cursor_chain_failed = await _collect_older_api_pages(
                    latest_payload,
                    fetch_older_page,
                    on_page=stage_older_page,
                )
            else:
                older_pages, cursor_chain_failed = await _collect_older_api_pages(
                    latest_payload,
                    fetch_older_page,
                )

            latest_info = latest_payload.get("page_info") if isinstance(latest_payload.get("page_info"), dict) else {}
            if latest_info.get("has_next_page") is not False:
                cursor_chain_failed = True

            attachment_scan: dict[str, Any] = {
                "performed": False,
                "stop_reason": "api_reference_inventory",
                "stable": None,
            }
            interpreter_metadata_quiet = True
            metadata_resource_urls: list[str] = []
            performance_resource_error: str | None = None
            metadata_refresh_count = 0
            file_download_refresh_count = 0
            current_url = urlparse(page.url)
            if current_url.path.rstrip("/") != parsed.path.rstrip("/"):
                cursor_chain_failed = True

            if message_spool is not None:
                combined = message_spool.finalize()
                normalized = {
                    "conversation_url": conversation_url,
                    "messages": message_spool,
                    "attachments": combined["attachments"],
                    "excluded_system_message_count": combined["excluded_system_message_count"],
                    "branch_selection": "provider_page_order",
                }
                for event in page_events:
                    event["payload"] = None
                latest_payload = None
            else:
                combined = self._combine_api_pages(latest_payload, older_pages)
                raw_messages = combined["messages"]
                normalized = self._normalise_api_conversation(
                    {"messages": raw_messages},
                    conversation_url,
                )
            latest_request_headers = latest_event.get("request_headers")
            sandbox_attachment_references = _sandbox_attachment_references(normalized["messages"])
            sandbox_attachment_metadata_resolved_count = 0
            sandbox_attachment_metadata_failure_count = 0
            for message_id, sandbox_path in sandbox_attachment_references:
                source_url = (
                    f"{parsed.scheme}://{parsed.netloc}/backend-api/conversation/{conversation_id}/interpreter/download?"
                    f"{urlencode({'message_id': message_id, 'sandbox_path': sandbox_path})}"
                )
                matching = [
                    item
                    for item in interpreter_attachments
                    if item.get("message_id") == message_id and item.get("sandbox_path") == sandbox_path
                ]
                if any(item.get("downloadable") is True for item in matching):
                    sandbox_attachment_metadata_resolved_count += 1
                    continue

                attempt = {
                    "route": "interpreter_message_reference_refresh",
                    "query_keys": ["message_id", "sandbox_path"],
                }
                api_attempts.append(attempt)
                if not isinstance(latest_request_headers, dict):
                    metadata_error = "request_context_unavailable"
                    refreshed = {"status": None, "payload": None}
                else:
                    try:
                        client_headers = _client_api_headers(
                            latest_request_headers,
                            urlparse(source_url).path,
                        )
                        metadata_refresh_count += 1
                        refreshed = await _authenticated_page_json_get(page, source_url, client_headers)
                    except ValueError:
                        metadata_error = "request_context_unavailable"
                        refreshed = {"status": None, "payload": None}
                    else:
                        attempt["status"] = refreshed.get("status")
                        for retry_attempt in refreshed.get("attempts", []):
                            api_attempts.append(
                                {
                                    "route": "interpreter_message_reference_refresh",
                                    "query_keys": ["message_id", "sandbox_path"],
                                    **retry_attempt,
                                }
                            )
                        metadata_error = None
                        payload = refreshed.get("payload")
                        if refreshed.get("status") != 200 or not isinstance(payload, dict):
                            metadata_error = "metadata_refresh_failed"
                        else:
                            metadata_error = None
                            record_interpreter_metadata(source_url, payload, attempt)

                resolved = any(
                    item.get("message_id") == message_id
                    and item.get("sandbox_path") == sandbox_path
                    and item.get("downloadable") is True
                    for item in interpreter_attachments
                )
                if resolved:
                    sandbox_attachment_metadata_resolved_count += 1
                    attempt["metadata_status"] = "success"
                    continue

                sandbox_attachment_metadata_failure_count += 1
                if metadata_error:
                    attempt["error_type"] = metadata_error
                    interpreter_failures += 1
                stable_attachment_id = "interpreter-" + uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{conversation_id}\0{message_id}\0{sandbox_path}",
                ).hex[:16]
                unavailable = next(
                    (
                        item
                        for item in interpreter_attachments
                        if item.get("message_id") == message_id and item.get("sandbox_path") == sandbox_path
                    ),
                    None,
                )
                if unavailable is None:
                    interpreter_attachments.append(
                        {
                            "attachment_id": stable_attachment_id,
                            "id": stable_attachment_id,
                            "file_id": None,
                            "message_id": message_id,
                            "filename": unquote(sandbox_path.rsplit("/", 1)[-1]),
                            "mime_type": None,
                            "size": None,
                            "source_url": None,
                            "file_kind": "interpreter_output",
                            "downloadable": False,
                            "unavailable_reason": "ChatGPT did not expose file download metadata",
                            "sandbox_path": sandbox_path,
                        }
                    )
                else:
                    unavailable.update(
                        {
                            "source_url": None,
                            "downloadable": False,
                            "unavailable_reason": "ChatGPT did not expose file download metadata",
                        }
                    )

            interpreter_metadata_quiet = sandbox_attachment_metadata_failure_count == 0
            resolved_api_refs: dict[tuple[str, str], str] = {}
            for item in normalized.get("attachments") or []:
                if not isinstance(item, dict) or item.get("library_file_id"):
                    continue
                attachment_id = item.get("file_id") or item.get("fileId") or item.get("id")
                filename = item.get("filename") or item.get("file_name") or item.get("name")
                size = item.get("size") or item.get("file_size_bytes")
                message_id = str(item.get("message_id") or "")
                if not isinstance(attachment_id, str) or not attachment_id:
                    continue
                if (
                    not isinstance(filename, str)
                    or not filename
                    or type(size) is not int
                    or size < 0
                ):
                    api_attachment_metadata_failure_count += 1
                    api_attempts.append(
                        {
                            "route": "api_attachment_metadata_refresh",
                            "error_type": "reference_metadata_missing",
                        }
                    )
                    continue
                ref_key = (message_id, attachment_id)
                source_url = resolved_api_refs.get(ref_key)
                if source_url is None:
                    source_url = (
                        f"{parsed.scheme}://{parsed.netloc}/backend-api/files/download/"
                        f"{quote(attachment_id, safe='')}?"
                        f"{urlencode({'conversation_id': conversation_id, 'inline': 'true'})}"
                    )
                    attempt = {
                        "route": "api_attachment_metadata_refresh",
                        "query_keys": ["conversation_id", "inline"],
                    }
                    api_attempts.append(attempt)
                    api_attachment_metadata_attempt_count += 1
                    if not isinstance(latest_request_headers, dict):
                        metadata_error = "request_context_unavailable"
                        refreshed = {"status": None, "payload": None}
                    else:
                        try:
                            client_headers = _client_api_headers(
                                latest_request_headers,
                                urlparse(source_url).path,
                            )
                            refreshed = await _authenticated_page_json_get(page, source_url, client_headers)
                        except ValueError:
                            metadata_error = "request_context_unavailable"
                            refreshed = {"status": None, "payload": None}
                        else:
                            attempt["status"] = refreshed.get("status")
                            for retry_attempt in refreshed.get("attempts", []):
                                api_attempts.append(
                                    {
                                        "route": "api_attachment_metadata_refresh",
                                        "query_keys": ["conversation_id", "inline"],
                                        **retry_attempt,
                                    }
                                )
                            metadata_error = _api_attachment_metadata_error(
                                source_url,
                                conversation_url,
                                attachment_id,
                                filename,
                                size,
                                refreshed.get("payload"),
                            )
                    if metadata_error is None:
                        resolved_api_refs[ref_key] = source_url
                        api_attachment_metadata_resolved_count += 1
                        attempt["metadata_status"] = "success"
                        attempt["download_url_present"] = True
                    else:
                        api_attachment_metadata_failure_count += 1
                        attempt["error_type"] = metadata_error
                        continue

                item.update(
                    {
                        "attachment_id": item.get("attachment_id") or attachment_id,
                        "file_id": attachment_id,
                        "filename": filename,
                        "size": size,
                        "source_url": source_url,
                        "file_kind": "api_attachment",
                        "downloadable": True,
                    }
                )
                if message_spool is None:
                    for message in normalized["messages"]:
                        if str(message.get("id") or "") != message_id:
                            continue
                        for message_item in message.get("attachments") or []:
                            if not isinstance(message_item, dict) or message_item.get("library_file_id"):
                                continue
                            if attachment_id not in {
                                str(message_item.get("id") or ""),
                                str(message_item.get("file_id") or ""),
                                str(message_item.get("fileId") or ""),
                            }:
                                continue
                            message_item.update(
                                {
                                    "attachment_id": message_item.get("attachment_id") or attachment_id,
                                    "file_id": attachment_id,
                                    "filename": filename,
                                    "size": size,
                                    "source_url": source_url,
                                    "file_kind": "api_attachment",
                                    "downloadable": True,
                                }
                            )
            pointer_file_ids = {
                file_id
                for message in normalized["messages"]
                for part in (message.get("rich_parts") or [])
                if isinstance(part, dict)
                for file_id in [_asset_pointer_file_id(part)]
                if file_id
            }
            pointer_metadata_attempt_count = 0
            pointer_metadata_resolved_count = 0
            pointer_metadata_failure_count = 0
            pointer_success_ids = {
                str(item.get("file_id"))
                for item in file_download_responses
                if item.get("status") == "success" and item.get("has_download_url")
            }
            for file_id in sorted(pointer_file_ids - pointer_success_ids):
                source_url = (
                    f"{parsed.scheme}://{parsed.netloc}/backend-api/files/download/"
                    f"{quote(file_id, safe='')}?download_intent=false&inline=false"
                )
                attempt = {
                    "route": "asset_pointer_metadata_refresh",
                    "query_keys": ["download_intent", "inline"],
                }
                api_attempts.append(attempt)
                pointer_metadata_attempt_count += 1
                if not isinstance(latest_request_headers, dict):
                    pointer_metadata_failure_count += 1
                    attempt["error_type"] = "request_context_unavailable"
                    continue
                try:
                    client_headers = _client_api_headers(latest_request_headers, urlparse(source_url).path)
                except ValueError:
                    pointer_metadata_failure_count += 1
                    attempt["error_type"] = "request_context_unavailable"
                    continue
                refreshed = await _authenticated_page_json_get(page, source_url, client_headers)
                attempt["status"] = refreshed.get("status")
                for retry_attempt in refreshed.get("attempts", []):
                    api_attempts.append(
                        {
                            "route": "asset_pointer_metadata_refresh",
                            "query_keys": ["download_intent", "inline"],
                            **retry_attempt,
                        }
                    )
                record_file_download_metadata(
                    source_url,
                    refreshed.get("status"),
                    refreshed.get("payload"),
                    attempt,
                )
                if attempt.get("metadata_status") == "success":
                    file_download_response_file_ids.add(file_id)
                    pointer_success_ids.add(file_id)
                    pointer_metadata_resolved_count += 1
                else:
                    pointer_metadata_failure_count += 1

            file_download_refresh_count = pointer_metadata_attempt_count
            file_resource_urls: list[str] = []
            file_route_attachments = _file_download_attachments_from_responses(
                normalized["messages"],
                file_download_responses,
            )
            attachment_scan["expected_api_message_count"] = len(normalized["messages"])
            attachment_scan["rendered_count_reaches_api_count"] = None
            attachments = (
                list(normalized.get("attachments", []))
                + file_route_attachments
                + interpreter_attachments
            )
            api_attachment_reference_summary = _api_attachment_reference_summary(
                normalized["messages"],
                [item for item in normalized.get("attachments", []) if isinstance(item, dict)],
            )
            pagination = combined["pagination"]
            messages_state = pagination["completeness"]
            if cursor_chain_failed:
                messages_state = "partial"
            library_file_reference_count = sum(
                isinstance(item, dict) and bool(item.get("library_file_id"))
                for item in normalized.get("attachments") or []
            )
            api_attachment_unavailable_count = sum(
                isinstance(item, dict)
                and not item.get("library_file_id")
                and item.get("downloadable") is not True
                for item in normalized.get("attachments") or []
            )
            pointer_unavailable_count = len(pointer_file_ids - pointer_success_ids)
            unclassified_file_part_count = sum(
                1
                for message in normalized["messages"]
                for part in (message.get("rich_parts") or [])
                if isinstance(part, dict)
                and any(part.get(key) for key in ("file_id", "fileId", "asset_pointer", "sandbox_path", "library_file_id"))
                and _asset_pointer_file_id(part) is None
            )
            attachment_inventory_failure_count = (
                library_file_reference_count
                + api_attachment_unavailable_count
                + pointer_unavailable_count
                + sandbox_attachment_metadata_failure_count
                + unclassified_file_part_count
            )
            attachments_state = (
                "partial"
                if interpreter_failures or attachment_inventory_failure_count or not interpreter_metadata_quiet
                else "verified"
            )
            overall = "verified" if messages_state == attachments_state == "verified" else (
                "partial" if "partial" in {messages_state, attachments_state} else "unverified"
            )
            result = {
                "conversation_url": conversation_url,
                "messages": normalized["messages"],
                "attachments": attachments,
                "source": "api_paged",
                "completeness": {
                    "messages": messages_state,
                    "attachments": attachments_state,
                    "overall": overall,
                },
                "reason": {
                    "messages": "api_page_chain_complete" if messages_state == "verified" else pagination["reason"],
                    "attachments": (
                        "attachment_metadata_failed"
                        if interpreter_failures or sandbox_attachment_metadata_failure_count
                        else "library_file_source_unavailable"
                        if library_file_reference_count
                        else "attachment_source_unavailable"
                        if attachment_inventory_failure_count
                        else "api_reference_inventory_complete"
                    ),
                },
                "provenance": {
                    "api_attempts": api_attempts,
                    "api_page_count": pagination["page_count"],
                    "api_raw_message_count": pagination["raw_message_count"],
                    "duplicate_message_count": pagination["duplicate_count"],
                    "excluded_system_message_count": normalized.get("excluded_system_message_count", 0),
                    "branch_selection": "provider_page_order_with_branch_metadata",
                    "interpreter_metadata_count": len(interpreter_attachments),
                    "interpreter_metadata_failures": interpreter_failures,
                    "interpreter_metadata_response_count": attachment_activity["response_count"],
                    "interpreter_metadata_quiet": interpreter_metadata_quiet,
                    "interpreter_metadata_resource_count": len(sandbox_attachment_references),
                    "interpreter_metadata_refresh_count": metadata_refresh_count,
                    "interpreter_metadata_resource_error": performance_resource_error,
                    "sandbox_attachment_reference_count": len(sandbox_attachment_references),
                    "sandbox_attachment_metadata_resolved_count": sandbox_attachment_metadata_resolved_count,
                    "sandbox_attachment_metadata_failure_count": sandbox_attachment_metadata_failure_count,
                    "api_attachment_reference_summary": api_attachment_reference_summary,
                    "api_attachment_metadata_attempt_count": api_attachment_metadata_attempt_count,
                    "api_attachment_metadata_resolved_count": api_attachment_metadata_resolved_count,
                    "api_attachment_metadata_failure_count": api_attachment_metadata_failure_count,
                    "pointer_metadata_attempt_count": pointer_metadata_attempt_count,
                    "pointer_metadata_resolved_count": pointer_metadata_resolved_count,
                    "pointer_metadata_failure_count": pointer_metadata_failure_count,
                    "attachment_inventory": {
                        "source": "complete_api_message_references",
                        "api_attachment_unavailable_count": api_attachment_unavailable_count,
                        "library_file_unavailable_count": library_file_reference_count,
                        "pointer_unavailable_count": pointer_unavailable_count,
                        "sandbox_reference_count": len(sandbox_attachment_references),
                        "sandbox_reference_resolved_count": sandbox_attachment_metadata_resolved_count,
                        "unclassified_file_part_count": unclassified_file_part_count,
                    },
                    "file_download_metadata_count": len(file_download_responses),
                    "file_download_reference_count": len(file_route_attachments),
                    "file_download_metadata_attempt_count": sum(
                        item.get("route") == "file_download_metadata" for item in api_attempts
                    ),
                    "file_download_resource_count": len(file_resource_urls),
                    "file_download_refresh_count": file_download_refresh_count,
                    "attachment_scan": {
                        key: attachment_scan[key]
                        for key in (
                            "performed",
                            "stop_reason",
                            "iterations",
                            "stable_rounds",
                            "rendered_message_count",
                            "observed_message_id_count",
                            "expected_api_message_count",
                            "rendered_count_reaches_api_count",
                            "scroller_count",
                            "max_scroll",
                            "reached_end",
                            "stop_reason",
                            "stable",
                            "error_type",
                            "scan_pass_count",
                            "ui_page_response_count",
                            "ui_older_page_response_count",
                            "ui_page_message_count",
                        )
                        if key in attachment_scan
                    },
                    "pagination": pagination,
                },
            }
            if not result["messages"]:
                result["error_code"] = "NO_MESSAGES_READ"
            if message_spool is not None:
                result["message_spool"] = message_spool
                spool_transferred = True
            return result
        finally:
            page.remove_listener("response", on_response)
            if pending:
                await asyncio.gather(*tuple(pending), return_exceptions=True)
            try:
                await page.close()
            except PlaywrightError:
                pass
            if message_spool is not None and not spool_transferred:
                message_spool.close()

    @staticmethod
    def _normalise_api_message(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
        author = raw.get("author") or {}
        role = raw.get("role") or (author.get("role") if isinstance(author, dict) else None)
        if role not in {"user", "assistant", "tool"}:
            return None
        content = raw.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else content
        text_parts: list[str] = []
        rich_parts: list[dict[str, Any]] = []
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, (str, int, float)):
                    text_parts.append(str(part))
                elif isinstance(part, dict):
                    value = part.get("text") or part.get("content") or part.get("value")
                    if isinstance(value, (str, int, float)):
                        text_parts.append(str(value))
                    rich_parts.append(part)
        elif isinstance(parts, (str, int, float)):
            text_parts.append(str(parts))
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        message_id = str(raw.get("id") or raw.get("message_id") or f"msg_{index + 1}")
        raw_attachments = metadata.get("attachments") or metadata.get("files") or raw.get("attachments") or []
        attachments: list[dict[str, Any]] = []
        if isinstance(raw_attachments, list):
            for item in raw_attachments:
                if not isinstance(item, dict):
                    continue
                library_file_id = item.get("library_file_id") or item.get("libraryFileId")
                explicit_file_id = item.get("file_id") or item.get("fileId")
                raw_id = item.get("id")
                file_id = explicit_file_id or library_file_id
                if (
                    not explicit_file_id
                    and library_file_id
                    and raw_id
                    and str(raw_id) != str(library_file_id)
                ):
                    file_id = raw_id
                attachment_id = item.get("attachment_id") or library_file_id or raw_id or file_id
                source_url = item.get("source_url") or item.get("sourceUrl") or item.get("url") or item.get("href")
                attachments.append(
                    {
                        **item,
                        "library_file_id": str(library_file_id) if library_file_id else None,
                        "attachment_id": str(attachment_id) if attachment_id else None,
                        "file_id": str(file_id) if file_id else None,
                        "filename": item.get("filename") or item.get("file_name") or item.get("name") or "attachment",
                        "mime_type": item.get("mime_type") or item.get("mimeType"),
                        "size": item.get("size") or item.get("file_size_bytes"),
                        "source_url": source_url,
                        "message_id": item.get("message_id") or message_id,
                    }
                )
        citations = metadata.get("citations") or metadata.get("content_references") or []
        return {
            **raw,
            "id": message_id,
            "role": role,
            "order": index,
            "created_at": raw.get("create_time"),
            "text": "\n".join(text_parts),
            "rich_parts": rich_parts,
            "channel": raw.get("channel"),
            "message_type": metadata.get("message_type"),
            "parent_id": metadata.get("parent_id"),
            "turn_exchange_id": metadata.get("turn_exchange_id"),
            "working_turn_id": metadata.get("working_turn_id"),
            "is_visually_hidden_from_conversation": bool(metadata.get("is_visually_hidden_from_conversation")),
            "citations": citations if isinstance(citations, list) else [citations],
            "attachments": attachments,
            "canvas": metadata.get("canvas") or metadata.get("canvas_document"),
            "deep_research": metadata.get("deep_research") or metadata.get("content_references"),
            "code_blocks": [],
            "tables": [],
        }

    @staticmethod
    def _normalise_api_conversation(payload: Any, conversation_url: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"conversation_url": conversation_url, "messages": [], "attachments": []}
        if not isinstance(payload.get("mapping"), dict) and isinstance(payload.get("messages"), list):
            raw_messages = payload.get("messages", [])
            messages = [
                normalized
                for index, raw in enumerate(raw_messages)
                if isinstance(raw, dict)
                for normalized in [ChatGPTBrowser._normalise_api_message(raw, index)]
                if normalized is not None
            ]
            excluded_system_count = sum(
                1
                for item in raw_messages
                if isinstance(item, dict)
                and isinstance(item.get("author"), dict)
                and item["author"].get("role") == "system"
            )
            raw_attachments = payload.get("attachments")
            if isinstance(raw_attachments, list):
                attachments = [item for item in raw_attachments if isinstance(item, dict)]
            else:
                attachments = [
                    {**item, "message_id": item.get("message_id") or message["id"]}
                    for message in messages
                    for item in message.get("attachments", [])
                    if isinstance(item, dict)
                ]
            return {
                "conversation_url": conversation_url,
                "messages": messages,
                "attachments": attachments,
                "current_node": payload.get("current_node"),
                "page_info": payload.get("page_info"),
                "branch_selection": "provider_page_order",
                "excluded_system_message_count": excluded_system_count,
            }
        mapping = payload.get("mapping")
        messages: list[dict[str, Any]] = []
        branch_truncated = False
        branch_selection = "source_order"
        if isinstance(mapping, dict):
            current_node = payload.get("current_node")
            if isinstance(current_node, dict):
                current_node = current_node.get("id")
            nodes: list[dict[str, Any]] = []
            if isinstance(current_node, str) and current_node in mapping:
                branch_selection = "active_parent_chain"
                reverse_chain: list[dict[str, Any]] = []
                seen_nodes: set[str] = set()
                node_id: str | None = current_node
                while node_id and node_id in mapping and node_id not in seen_nodes:
                    seen_nodes.add(node_id)
                    node = mapping[node_id]
                    if not isinstance(node, dict):
                        break
                    reverse_chain.append(node)
                    parent = node.get("parent")
                    if isinstance(parent, str) and parent not in mapping:
                        branch_truncated = True
                        node_id = None
                    else:
                        node_id = parent if isinstance(parent, str) else None
                nodes = list(reversed(reverse_chain))
            if not nodes:
                nodes = [node for node in mapping.values() if isinstance(node, dict)]
            seen_messages: set[str] = set()
            for index, node in enumerate(nodes):
                if not isinstance(node, dict) or not isinstance(node.get("message"), dict):
                    continue
                raw = node["message"]
                author = raw.get("author") or {}
                role = author.get("role") if isinstance(author, dict) else None
                if role not in {"user", "assistant", "tool"}:
                    continue
                message_id = str(raw.get("id") or node.get("id") or f"msg_{index + 1}")
                if message_id in seen_messages:
                    continue
                seen_messages.add(message_id)
                content = raw.get("content") or {}
                parts = content.get("parts") if isinstance(content, dict) else content
                if isinstance(parts, list):
                    text = "\n".join(str(part) for part in parts if isinstance(part, (str, int, float)))
                    rich_parts = [part for part in parts if isinstance(part, dict)]
                else:
                    text = str(parts or "")
                    rich_parts = []
                metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
                citations = metadata.get("citations") or metadata.get("content_references") or []
                attachments = metadata.get("attachments") or metadata.get("files") or []
                messages.append(
                    {
                        "id": message_id,
                        "role": role,
                        "order": len(messages),
                        "created_at": raw.get("create_time"),
                        "text": text,
                        "rich_parts": rich_parts,
                        "citations": citations,
                        "attachments": attachments,
                        "code_blocks": [],
                        "tables": [],
                        "canvas": metadata.get("canvas") or metadata.get("canvas_document"),
                        "deep_research": metadata.get("deep_research") or metadata.get("content_references"),
                    }
                )
        attachments: list[dict[str, Any]] = []
        top_level_attachments = payload.get("attachments")
        if isinstance(top_level_attachments, list):
            attachments.extend(item for item in top_level_attachments if isinstance(item, dict))
        else:
            for message in messages:
                for item in message.get("attachments") or []:
                    if isinstance(item, dict):
                        attachments.append({**item, "message_id": item.get("message_id") or message["id"]})
        return {
            "conversation_url": conversation_url,
            "messages": messages,
            "attachments": attachments,
            "branch_truncated": branch_truncated,
            "branch_selection": branch_selection,
        }

    async def _extract_dom_conversation(self, page: Page, conversation_url: str) -> dict[str, Any]:
        script = r"""
        () => {
          const marked = Array.from(document.querySelectorAll('[data-message-author-role]'));
          const nodes = marked.length ? marked : Array.from(document.querySelectorAll('article'));
          const messages = [];
          for (const node of nodes) {
            const role = node.getAttribute('data-message-author-role') ||
              (node.matches('article') && /assistant|user/i.test(node.getAttribute('data-testid') || '')
                ? ((node.getAttribute('data-testid') || '').toLowerCase().includes('assistant') ? 'assistant' : 'user') : null);
            if (!role || !['user','assistant','system','tool'].includes(role)) continue;
            const text = (node.innerText || node.textContent || '').trim();
            const id = node.getAttribute('data-message-id') || node.getAttribute('data-testid') || ('msg_' + (messages.length + 1));
            const links = Array.from(node.querySelectorAll('a[href]')).map(a => ({label: (a.innerText || '').trim(), url: a.href}));
            const code_blocks = Array.from(node.querySelectorAll('pre')).map(el => ({language: el.querySelector('code')?.className || null, text: (el.innerText || el.textContent || '')}));
            const tables = Array.from(node.querySelectorAll('table')).map(el => ({text: (el.innerText || '').trim()}));
            const resources = Array.from(node.querySelectorAll('a[href], img[src]')).filter(el => {
              if (el.tagName === 'IMG') {
                if (!el.src || el.src.startsWith('data:') || /google\.com\/s2\/favicons/i.test(el.src)) return false;
                const owner = el.closest('a[href]');
                return !owner || owner.href === el.src;
              }
              const href = el.href || '';
              const label = el.getAttribute('download') || el.getAttribute('aria-label') || (el.innerText || '').trim();
              return Boolean(el.getAttribute('download')) ||
                /(?:oaiusercontent\.com|\/backend-api\/(?:files|assets)|\/files\/)/i.test(href) ||
                /\.(?:pdf|zip|jsonl?|csv|txt|md|docx?|xlsx?|pptx?|png|jpe?g|gif|webp)(?:[?#]|$)/i.test(href + ' ' + label);
            });
            const attachments = resources.map(el => ({
              source_url: el.href || el.src,
              filename: el.getAttribute('download') || el.getAttribute('aria-label') || (el.innerText || '').trim() || null,
              mime_type: null,
              size: null,
              message_id: id
            })).filter(item => item.source_url && !item.source_url.startsWith('javascript:'));
            for (const tile of node.querySelectorAll('[role="group"][aria-label]')) {
              if (!(String(tile.className).includes('file-tile') || tile.querySelector('[data-testid="library-file-icon"]'))) continue;
              const filename = tile.getAttribute('aria-label');
              if (!filename) continue;
              const kind = tile.querySelector('[data-library-file-icon-kind]')?.getAttribute('data-library-file-icon-kind');
              let fileProps = null;
              const fiberKey = Object.keys(tile).find(key => key.startsWith('__reactFiber'));
              let fiber = fiberKey ? tile[fiberKey] : null;
              for (let depth = 0; fiber && depth < 12; depth++, fiber = fiber.return) {
                const props = fiber.memoizedProps || {};
                if (props.fileId) {
                  fileProps = props;
                  break;
                }
              }
              attachments.push({source_url: null,
                file_id: fileProps?.fileId || null,
                filename: fileProps?.file?.name || filename,
                mime_type: fileProps?.mimeType || (kind && kind.includes('/') ? kind : null),
                downloadable: fileProps?.capabilities?.downloadable || null,
                file_kind: kind || null, size: fileProps?.file?.size || null,
                message_id: id, unavailable_reason: 'Rendered file receipt does not expose a source URL'});
            }
            const canvas = node.querySelector('[data-testid*="canvas"], [class*="canvas"]');
            messages.push({id, role, text, citations: links, code_blocks, tables,
              canvas: canvas ? {text: (canvas.innerText || '').trim()} : null,
              attachments});
          }
          return {messages, attachments: messages.flatMap(item => item.attachments || [])};
        }
        """
        try:
            value = await page.evaluate(script)
        except PlaywrightError as exc:
            raise BrowserTaskError("ChatGPT DOM extraction failed") from exc
        if not isinstance(value, dict):
            raise BrowserTaskError("ChatGPT DOM extraction returned an invalid payload")
        return {"conversation_url": conversation_url, "messages": value.get("messages", []), "attachments": value.get("attachments", [])}

    async def _load_all_rendered_messages(
        self,
        page: Page,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Walk the isolated conversation scroller to expose virtualized resources."""
        loop = asyncio.get_running_loop()
        local_deadline = loop.time() + min(30.0, max(5.0, self.settings.browser_timeout_seconds))
        if deadline is not None:
            local_deadline = min(local_deadline, deadline)
        observed_message_ids: set[str] = set()
        observed_message_scroll_tops: dict[str, float] = {}
        original_scroll_top: float | None = None
        previous_bottom_max: float | None = None
        previous_progress_state: tuple[float, float, int] | None = None
        stable_rounds = 0
        no_progress_rounds = 0
        rendered_message_count = 0
        scroller_count = 0
        scroll_top = 0.0
        max_scroll = 0.0
        reached_end = False
        stop_reason = "step_limit"

        async def move_scroller(top: float) -> dict[str, Any]:
            return await page.evaluate(
                """async requestedTop => {
                  const hasMessages = el => Boolean(el.querySelector('[data-message-author-role], article'));
                  const scrollers = [...document.querySelectorAll('*')].filter(el => {
                    const overflow = getComputedStyle(el).overflowY;
                    return (overflow === 'auto' || overflow === 'scroll' || overflow === 'overlay') &&
                      el.scrollHeight > el.clientHeight + 8 && hasMessages(el);
                  });
                  scrollers.sort((a, b) =>
                    (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
                  );
                  const scroller = scrollers[0];
                  if (!scroller) return {scroller_count: 0};
                  const originalTop = scroller.scrollTop;
                  const maxBefore = scroller.scrollHeight - scroller.clientHeight;
                  scroller.scrollTop = Math.min(Math.max(requestedTop, 0), maxBefore);
                  scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
                  await new Promise(resolve => setTimeout(resolve, 120));
                  const nodes = [...document.querySelectorAll('[data-message-author-role], article')];
                  const messageIds = [...document.querySelectorAll('[data-message-id]')]
                    .map(node => node.getAttribute('data-message-id'))
                    .filter(Boolean);
                  return {
                    scroller_count: scrollers.length,
                    original_scroll_top: originalTop,
                    scroll_top: scroller.scrollTop,
                    max_scroll: scroller.scrollHeight - scroller.clientHeight,
                    client_height: scroller.clientHeight,
                    rendered_message_count: nodes.length,
                    message_ids: messageIds,
                  };
                }""",
                top,
            )

        iterations = 0
        step = 450.0
        try:
            for iterations in range(1, 2001):
                if loop.time() >= local_deadline:
                    stop_reason = "deadline"
                    break
                state = await move_scroller(scroll_top)
                scroller_count = int(state.get("scroller_count") or 0)
                if scroller_count == 0:
                    stop_reason = "scroller_not_found"
                    break
                if original_scroll_top is None:
                    original_scroll_top = float(state.get("original_scroll_top") or 0)
                scroll_top = float(state.get("scroll_top") or 0)
                for message_id in state.get("message_ids", []):
                    if isinstance(message_id, str) and message_id:
                        observed_message_ids.add(message_id)
                        observed_message_scroll_tops.setdefault(message_id, scroll_top)
                rendered_message_count = max(
                    rendered_message_count,
                    int(state.get("rendered_message_count") or 0),
                )
                max_scroll = float(state.get("max_scroll") or 0)
                step = max(450.0, float(state.get("client_height") or 0) * 0.75)
                progress_state = (scroll_top, max_scroll, len(observed_message_ids))
                if progress_state == previous_progress_state:
                    no_progress_rounds += 1
                else:
                    no_progress_rounds = 0
                    previous_progress_state = progress_state

                if no_progress_rounds >= 24 and scroll_top < max_scroll - 2:
                    stop_reason = "no_scroll_progress"
                    break

                if scroll_top >= max_scroll - 2:
                    stable_rounds = stable_rounds + 1 if previous_bottom_max == max_scroll else 0
                    previous_bottom_max = max_scroll
                    if stable_rounds >= 3:
                        reached_end = True
                        stop_reason = "reached_end"
                        break
                else:
                    stable_rounds = 0
                    previous_bottom_max = None
                scroll_top = min(scroll_top + step, max_scroll)
        finally:
            if original_scroll_top is not None:
                try:
                    await move_scroller(original_scroll_top)
                except PlaywrightError:
                    pass

        return {
            "iterations": iterations,
            "stable_rounds": stable_rounds,
            "no_progress_rounds": no_progress_rounds,
            "rendered_message_count": rendered_message_count,
            "observed_message_id_count": len(observed_message_ids),
            "observed_message_ids": sorted(observed_message_ids),
            "observed_message_scroll_tops": observed_message_scroll_tops,
            "scroller_count": scroller_count,
            "max_scroll": int(max_scroll),
            "reached_end": reached_end,
            "stop_reason": stop_reason,
            "stable": reached_end,
        }

    async def _stream_content_to_path(
        self,
        source_url: str,
        destination: Path,
        *,
        max_file_bytes: int,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> StreamedAttachment:
        url = source_url
        timeout = httpx.Timeout(self.settings.browser_timeout_seconds)
        cookie_jar = httpx.Cookies()
        if self._context is not None:
            try:
                browser_cookies = await self._context.cookies(url)
            except PlaywrightError as exc:
                raise BrowserTaskError("Could not access the logged-in file download context") from exc
            for cookie in browser_cookies:
                name = cookie.get("name")
                value = cookie.get("value")
                domain = cookie.get("domain")
                if isinstance(name, str) and isinstance(value, str) and isinstance(domain, str) and domain:
                    cookie_jar.set(name, value, domain=domain, path=str(cookie.get("path") or "/"))

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            cookies=cookie_jar,
        ) as client:
            for _ in range(6):
                parsed = urlparse(url)
                if parsed.scheme != "https" or not _is_chatgpt_download_host(parsed.hostname or ""):
                    raise BrowserTaskError("ChatGPT file redirect is outside the verified download hosts")
                async with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise BrowserTaskError("ChatGPT file redirect is missing its destination")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise BrowserTaskError(f"ChatGPT file content returned HTTP {response.status_code}")
                    declared_length = response.headers.get("content-length")
                    if declared_length:
                        try:
                            if int(declared_length) > max_file_bytes:
                                raise BrowserTaskError("ChatGPT file exceeds the configured size limit")
                        except ValueError as exc:
                            raise BrowserTaskError("ChatGPT returned an invalid Content-Length") from exc
                    total = 0
                    last_progress = 0
                    try:
                        with destination.open("wb") as file:
                            async for chunk in response.aiter_raw(self.settings.file_store_io_chunk_bytes):
                                if total + len(chunk) > max_file_bytes:
                                    raise BrowserTaskError("ChatGPT file exceeds the configured size limit")
                                file.write(chunk)
                                total += len(chunk)
                                if on_progress and total - last_progress >= 16 * 1024 * 1024:
                                    await on_progress(total)
                                    last_progress = total
                            file.flush()
                            os.fsync(file.fileno())
                        if on_progress and total != last_progress:
                            await on_progress(total)
                    except BaseException:
                        destination.unlink(missing_ok=True)
                        raise
                    if declared_length and total != int(declared_length):
                        destination.unlink(missing_ok=True)
                        raise BrowserTaskError("ChatGPT file size did not match Content-Length")
                    return StreamedAttachment(
                        total,
                        response.headers.get("content-type"),
                        response.headers.get("content-disposition"),
                    )
        raise BrowserTaskError("ChatGPT file exceeded the redirect limit")

    async def _download_file_card(
        self,
        page: Page,
        file_id: str,
        conversation_id: str,
        *,
        destination: Path | None = None,
        max_file_bytes: int | None = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[bytes, str | None, str | None] | StreamedAttachment:
        """Invoke ChatGPT's own file-card action and capture its content request."""
        responses: list[Any] = []

        def on_response(response: Any) -> None:
            url = str(response.url)
            if "/backend-api/" in url and (
                "/files/" in url or "/estuary/content" in url
            ):
                responses.append(response)

        page.on("response", on_response)
        try:
            action_script = r"""
            (fileId) => {
              const groups = Array.from(document.querySelectorAll('[role="group"][aria-label]'));
              for (const group of groups) {
                const fiberKey = Object.keys(group).find(key => key.startsWith('__reactFiber'));
                let fiber = fiberKey ? group[fiberKey] : null;
                for (let depth = 0; fiber && depth < 12; depth++, fiber = fiber.return) {
                  const props = fiber.memoizedProps || {};
                  if (props.fileId !== fileId) continue;
                  if (typeof props.onPreview === 'function') {
                    props.onPreview({kind: 'download'});
                    return true;
                  }
                }
              }
              return false;
            }
            """
            invoked = await page.evaluate(action_script, file_id)
            if not invoked:
                raise BrowserTaskError(f"ChatGPT file card is not currently rendered: {file_id}")
            for _ in range(60):
                if any("/backend-api/estuary/content" in str(item.url) for item in responses):
                    break
                await page.wait_for_timeout(250)
            if not any("/backend-api/estuary/content" in str(item.url) for item in responses):
                # A few ChatGPT builds attach the download handler to the
                # visible button rather than the onPreview callback.  Reuse
                # the exact card click as a second, still user-scoped path.
                groups = page.locator('[role="group"][aria-label]')
                for index in range(await groups.count()):
                    group = groups.nth(index)
                    try:
                        matches = await group.evaluate(
                            """(node, wanted) => {
                              const key = Object.keys(node).find(k => k.startsWith('__reactFiber'));
                              let fiber = key ? node[key] : null;
                              for (let depth = 0; fiber && depth < 12; depth++, fiber = fiber.return) {
                                if ((fiber.memoizedProps || {}).fileId === wanted) return true;
                              }
                              return false;
                            }""",
                            file_id,
                        )
                    except Exception:
                        matches = False
                    if matches:
                        try:
                            await group.locator("button").first.click(force=True, timeout=5000)
                        except PlaywrightError:
                            pass
                        break
                for _ in range(40):
                    if any("/backend-api/estuary/content" in str(item.url) for item in responses):
                        break
                    await page.wait_for_timeout(250)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

        estuary = next(
            (item for item in responses if "/backend-api/estuary/content" in str(item.url)),
            None,
        )
        if estuary is not None and estuary.status == 200:
            if destination is not None:
                return await self._stream_content_to_path(
                    str(estuary.url),
                    destination,
                    max_file_bytes=max_file_bytes or self.settings.file_store_max_file_bytes,
                    on_progress=on_progress,
                )
            return await estuary.body(), estuary.headers.get("content-type"), estuary.headers.get("content-disposition")

        # Some file variants return a JSON metadata envelope first.  Follow a
        # URL in that envelope without exposing its signed query to the client.
        for response in responses:
            if "/files/" not in str(response.url):
                continue
            try:
                metadata = await response.json()
            except Exception:
                continue
            candidate = self._find_content_url(metadata)
            if candidate:
                if destination is not None:
                    return await self._stream_content_to_path(
                        candidate,
                        destination,
                        max_file_bytes=max_file_bytes or self.settings.file_store_max_file_bytes,
                        on_progress=on_progress,
                    )
                fetched = await self._context.request.get(candidate, timeout=int(self.settings.browser_timeout_seconds * 1000))  # type: ignore[union-attr]
                if fetched.ok:
                    return await fetched.body(), fetched.headers.get("content-type"), fetched.headers.get("content-disposition")

        # If the browser served the URL from cache, Playwright may not emit a
        # response event.  The performance buffer still exposes the signed
        # estuary URL for this explicit download operation.
        try:
            cached_urls = await page.evaluate(
                """(fileId) => performance.getEntriesByType('resource').map(e => e.name)
                    .filter(name => name.includes('/backend-api/estuary/content') && name.includes('id=' + encodeURIComponent(fileId)))""",
                file_id,
            )
            for candidate in reversed(cached_urls or []):
                if destination is not None:
                    return await self._stream_content_to_path(
                        candidate,
                        destination,
                        max_file_bytes=max_file_bytes or self.settings.file_store_max_file_bytes,
                        on_progress=on_progress,
                    )
                fetched = await self._context.request.get(candidate, timeout=int(self.settings.browser_timeout_seconds * 1000))  # type: ignore[union-attr]
                if fetched.ok:
                    return await fetched.body(), fetched.headers.get("content-type"), fetched.headers.get("content-disposition")
        except Exception:
            pass
        raise BrowserTaskError(f"ChatGPT did not expose downloadable bytes for file: {file_id}")

    @staticmethod
    def _find_content_url(value: Any) -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and (
                    "/estuary/content" in item or "/files/download" in item
                ):
                    return item
                found = ChatGPTBrowser._find_content_url(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = ChatGPTBrowser._find_content_url(item)
                if found:
                    return found
        elif isinstance(value, str) and "/estuary/content" in value:
            return value
        return None

    async def _download_interpreter_attachment(
        self,
        source_url: str,
        attachment_id: str,
        *,
        destination: Path | None = None,
        max_file_bytes: int | None = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[bytes, str | None, str | None] | StreamedAttachment:
        assert self._context is not None
        source = urlparse(source_url)
        segments = [segment for segment in source.path.split("/") if segment]
        if (
            len(segments) != 5
            or segments[0:2] != ["backend-api", "conversation"]
            or segments[3:] != ["interpreter", "download"]
        ):
            raise BrowserTaskError("Interpreter attachment metadata route is invalid")
        conversation_id = segments[2]
        query = parse_qs(source.query)
        if not (query.get("message_id") or [None])[0] or not (query.get("sandbox_path") or [None])[0]:
            raise BrowserTaskError("Interpreter attachment metadata is missing stable identifiers")

        page = await self._context.new_page()
        try:
            conversation_path = f"/backend-api/conversations/{conversation_id}"
            conversation_url = f"{source.scheme}://{source.netloc}/c/{quote(conversation_id, safe='')}"
            try:
                async with page.expect_response(
                    lambda response: urlparse(response.url).path == conversation_path,
                    timeout=int(self.settings.browser_timeout_seconds * 1000),
                ) as response_info:
                    await page.goto(
                        conversation_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.settings.browser_timeout_seconds * 1000),
                    )
                conversation_response = await response_info.value
            except PlaywrightError as exc:
                raise BrowserTaskError("Could not refresh the logged-in conversation context") from exc

            if not conversation_response.ok:
                raise BrowserTaskError("Could not refresh the logged-in conversation context")
            request_headers = await conversation_response.request.all_headers()
            try:
                client_headers = _client_api_headers(request_headers, source.path)
            except ValueError as exc:
                raise BrowserTaskError("ChatGPT did not expose required file-request headers") from exc

            metadata_result = await _authenticated_page_json_get(page, source_url, client_headers)
            metadata = metadata_result.get("payload")
            if metadata_result.get("status") != 200 or not isinstance(metadata, dict):
                raise BrowserTaskError("ChatGPT could not refresh interpreter file metadata")
            if metadata.get("status") != "success":
                raise BrowserTaskError("ChatGPT did not make this interpreter file available")

            file_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
            actual_file_id = file_metadata.get("file_id")
            if (
                actual_file_id
                and not attachment_id.startswith("interpreter-")
                and str(actual_file_id) != attachment_id
            ):
                raise BrowserTaskError("Interpreter metadata returned a different file")

            download_url = metadata.get("download_url")
            if not isinstance(download_url, str) or not download_url:
                raise BrowserTaskError("ChatGPT did not expose interpreter file bytes")
            final_url = urljoin(source_url, download_url)
            final = urlparse(final_url)
            if (
                final.scheme != "https"
                or not _is_chatgpt_download_host(final.hostname or "")
                or not ("/estuary/content" in final.path or "/files/download/" in final.path)
            ):
                raise BrowserTaskError("Interpreter file URL is outside the observed download route")

            if destination is not None:
                return await self._stream_content_to_path(
                    final_url,
                    destination,
                    max_file_bytes=max_file_bytes or self.settings.file_store_max_file_bytes,
                    on_progress=on_progress,
                )

            try:
                response = await self._context.request.get(
                    final_url,
                    timeout=int(self.settings.browser_timeout_seconds * 1000),
                )
            except PlaywrightError as exc:
                raise BrowserTaskError("Interpreter file content request failed") from exc
            if not response.ok:
                raise BrowserTaskError(f"Interpreter file content returned HTTP {response.status}")
            return (
                await response.body(),
                response.headers.get("content-type"),
                response.headers.get("content-disposition"),
            )
        finally:
            try:
                await page.close()
            except PlaywrightError:
                pass

    async def _download_file_download_endpoint(
        self,
        page: Page,
        source_url: str,
        conversation_id: str,
        file_id: str,
        *,
        destination: Path | None = None,
        max_file_bytes: int | None = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[bytes, str | None, str | None] | StreamedAttachment | None:
        source = urlparse(source_url)
        conversation_path = f"/backend-api/conversations/{conversation_id}"
        conversation_url = f"{source.scheme}://{source.netloc}/c/{quote(conversation_id, safe='')}"
        try:
            async with page.expect_response(
                lambda response: urlparse(response.url).path == conversation_path and response.ok,
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            ) as response_info:
                await page.goto(
                    conversation_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.settings.browser_timeout_seconds * 1000),
                )
            conversation_response = await response_info.value
        except PlaywrightError as exc:
            raise BrowserTaskError("Could not refresh the logged-in conversation context") from exc
        try:
            request_headers = await conversation_response.request.all_headers()
            client_headers = _client_api_headers(request_headers, source.path)
        except (PlaywrightError, ValueError) as exc:
            raise BrowserTaskError("ChatGPT did not expose required file-request headers") from exc

        metadata_result = await _authenticated_page_json_get(page, source_url, client_headers)
        metadata = metadata_result.get("payload")
        if (
            metadata_result.get("status") != 200
            or not isinstance(metadata, dict)
            or metadata.get("status") != "success"
            or not isinstance(metadata.get("download_url"), str)
        ):
            return None
        file_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
        actual_file_id = file_metadata.get("file_id")
        if actual_file_id and str(actual_file_id) != file_id:
            raise BrowserTaskError("ChatGPT file metadata returned a different file ID")

        final_url = urljoin(source_url, metadata["download_url"])
        final = urlparse(final_url)
        if (
            final.scheme != "https"
            or not _is_chatgpt_download_host(final.hostname or "")
            or not ("/backend-api/estuary/content" in final.path or "/files/download/" in final.path)
        ):
            raise BrowserTaskError("ChatGPT file URL is outside the observed download route")
        if final.path == "/backend-api/estuary/content" and (
            (parse_qs(final.query).get("id") or [None])[0] != file_id
        ):
            raise BrowserTaskError("ChatGPT file metadata returned a different content ID")
        if destination is not None:
            return await self._stream_content_to_path(
                final_url,
                destination,
                max_file_bytes=max_file_bytes or self.settings.file_store_max_file_bytes,
                on_progress=on_progress,
            )
        assert self._context is not None
        try:
            response = await self._context.request.get(
                final_url,
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        except PlaywrightError as exc:
            raise BrowserTaskError("ChatGPT file content request failed") from exc
        if not response.ok:
            return None
        return (
            await response.body(),
            response.headers.get("content-type"),
            response.headers.get("content-disposition"),
        )

    async def download_attachment(
        self,
        source_url: str,
        *,
        attachment_id: str | None = None,
        conversation_url: str | None = None,
        destination: Path | None = None,
        max_file_bytes: int | None = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[bytes, str | None, str | None] | StreamedAttachment:
        """Fetch one attachment through the logged-in browser context.

        Legacy calls return the bounded-by-caller response bytes. Supplying a
        destination streams the observed signed content response into that
        file; cookies and signed URLs remain inside this browser component.
        """
        parsed = urlparse(source_url)
        host = parsed.hostname or ""
        allowed_host = _is_chatgpt_download_host(host)
        if parsed.scheme not in {"http", "https"} or not allowed_host:
            raise BrowserTaskError("attachment URL is outside the permitted ChatGPT domains")
        await self.start()
        assert self._context is not None
        if (
            parsed.path.startswith("/backend-api/conversation/")
            and parsed.path.endswith("/interpreter/download")
        ):
            if not attachment_id:
                raise BrowserTaskError("Interpreter download requires an attachment ID")
            return await self._download_interpreter_attachment(
                source_url,
                attachment_id,
                destination=destination,
                max_file_bytes=max_file_bytes,
                on_progress=on_progress,
            )
        if "/backend-api/files/download/" in parsed.path:
            file_id = unquote(parsed.path.rsplit("/", 1)[-1])
            conversation_id = (parse_qs(parsed.query).get("conversation_id") or [None])[0]
            if conversation_url:
                target = urlparse(conversation_url)
                if target.scheme not in {"http", "https"} or target.hostname not in {
                    "chatgpt.com",
                    "www.chatgpt.com",
                    "chat.openai.com",
                    "www.chat.openai.com",
                }:
                    raise BrowserTaskError("attachment conversation URL is outside ChatGPT")
                target_id = target.path.rstrip("/").rsplit("/", 1)[-1]
                if conversation_id and conversation_id != target_id:
                    raise BrowserTaskError("attachment source does not match its export conversation")
                conversation_id = target_id
            if not conversation_id:
                raise BrowserTaskError("ChatGPT file-card download is missing its conversation ID")
            page = await self._context.new_page()
            try:
                stream_options = (
                    {"destination": destination, "max_file_bytes": max_file_bytes, "on_progress": on_progress}
                    if destination is not None
                    else {}
                )
                direct = await self._download_file_download_endpoint(
                    page,
                    source_url,
                    conversation_id,
                    file_id,
                    **stream_options,
                )
                if direct is not None:
                    return direct
                await self._load_all_rendered_messages(page)
                return await self._download_file_card(page, file_id, conversation_id, **stream_options)
            finally:
                try:
                    await page.close()
                except PlaywrightError:
                    pass
        if destination is not None:
            if parsed.path != "/backend-api/estuary/content" and parsed.path.startswith("/backend-api/"):
                raise BrowserTaskError("This attachment source needs a verified streaming adapter")
            if parsed.path == "/backend-api/estuary/content" and attachment_id:
                content_id = (parse_qs(parsed.query).get("id") or [None])[0]
                if content_id != attachment_id:
                    raise BrowserTaskError("ChatGPT file source does not match the requested attachment")
            return await self._stream_content_to_path(
                source_url,
                destination,
                max_file_bytes=max_file_bytes or self.settings.file_store_max_file_bytes,
                on_progress=on_progress,
            )
        response = await self._context.request.get(source_url, timeout=int(self.settings.browser_timeout_seconds * 1000))
        if not response.ok:
            raise BrowserTaskError(f"ChatGPT attachment download failed with HTTP {response.status}")
        return await response.body(), response.headers.get("content-type"), response.headers.get("content-disposition")

    async def _upload_files(
        self,
        page: Page,
        uploads: list[InMemoryUpload | FileRecord],
    ) -> list[dict[str, Any]]:
        if not uploads:
            return []
        display_names = self._unique_display_names(uploads)
        if isinstance(uploads[0], FileRecord):
            if not all(isinstance(item, FileRecord) for item in uploads):
                raise BrowserTaskError("A single send cannot mix in-memory uploads and stored file references.")
            payloads: list[Any] = [item.path for item in uploads]
        else:
            if not all(isinstance(item, InMemoryUpload) for item in uploads):
                raise BrowserTaskError("A single send cannot mix in-memory uploads and stored file references.")
            payloads = [
                {"name": name, "mimeType": item.metadata.mime_type, "buffer": item.data}
                for item, name in zip(uploads, display_names)
            ]
        action = await self._attachment_action(page)
        uploaded_via = "filechooser"
        if action is not None:
            try:
                async with page.expect_file_chooser(timeout=5000) as chooser_info:
                    await action.click()
                chooser = await chooser_info.value
                await chooser.set_files(payloads)
            except PlaywrightError:
                uploaded_via = "input"
        else:
            uploaded_via = "input"
        if uploaded_via == "input":
            inputs = page.locator("input[type='file']")
            if await inputs.count() == 0:
                raise BrowserTaskError("找不到 ChatGPT 文件上传控件。")
            last_error: Exception | None = None
            for index in range(await inputs.count() - 1, -1, -1):
                try:
                    await inputs.nth(index).set_input_files(payloads)
                    last_error = None
                    break
                except PlaywrightError as exc:
                    last_error = exc
            if last_error:
                raise BrowserTaskError("ChatGPT 文件上传控件无法接收文件。") from last_error
        names: list[str] = []
        for name in display_names:
            locator = page.get_by_role(
                "button",
                name=re.compile(rf"(移除文件|Remove file).*{re.escape(name)}", re.I),
            )
            try:
                await locator.first.wait_for(state="visible", timeout=10000)
            except PlaywrightError:
                fallback = page.get_by_text(re.compile(re.escape(name), re.I))
                try:
                    await fallback.first.wait_for(state="visible", timeout=2000)
                except PlaywrightError as exc:
                    raise BrowserTaskError(f"文件已提交但页面没有显示附件：{name}") from exc
            names.append(name)
        receipts: list[dict[str, Any]] = []
        for item, name in zip(uploads, names):
            metadata = item.metadata.public() if isinstance(item, InMemoryUpload) else item.public()
            receipts.append(
                {
                    **metadata,
                    "displayed_filename": name,
                    "upload_method": uploaded_via,
                    "verified": True,
                }
            )
        return receipts

    @staticmethod
    def _unique_display_names(uploads: list[InMemoryUpload | FileRecord]) -> list[str]:
        counts: dict[str, int] = {}
        names: list[str] = []
        for item in uploads:
            original = item.metadata.filename if isinstance(item, InMemoryUpload) else item.filename
            key = original.casefold()
            counts[key] = counts.get(key, 0) + 1
            ordinal = counts[key]
            if ordinal == 1:
                names.append(original)
                continue
            stem = Path(original).stem or "upload"
            suffix = Path(original).suffix
            names.append(f"{stem} ({ordinal}){suffix}")
        return names

    async def _clear_uploaded_files(self, page: Page, names: list[str]) -> None:
        for name in self._unique_display_names_from_names(names):
            locator = page.get_by_role(
                "button",
                name=re.compile(rf"(移除文件|Remove file).*{re.escape(name)}", re.I),
            )
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible():
                        await candidate.click(timeout=3000)
                        break
                except PlaywrightError:
                    continue

    @staticmethod
    def _unique_display_names_from_names(names: list[str]) -> list[str]:
        counts: dict[str, int] = {}
        result: list[str] = []
        for original in names:
            key = original.casefold()
            counts[key] = counts.get(key, 0) + 1
            ordinal = counts[key]
            if ordinal == 1:
                result.append(original)
            else:
                stem = Path(original).stem or "upload"
                suffix = Path(original).suffix
                result.append(f"{stem} ({ordinal}){suffix}")
        return result

    async def _attachment_action(self, page: Page) -> Locator | None:
        # Open the visible composer attachment menu before waiting for the
        # chooser. This follows the tested Wangnov interaction sequence.
        plus = page.locator("button[data-testid='composer-plus-btn']")
        if await plus.count():
            for index in range(await plus.count() - 1, -1, -1):
                candidate = plus.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click()
                    await page.wait_for_timeout(200)
                    break
        patterns = (
            r"^(添加照片和文件|添加文件|Add photos and files|Add files|Upload from computer)$",
            r"(添加照片和文件|Add photos and files|Add files|Upload from computer)",
        )
        for pattern in patterns:
            for role in ("menuitem", "button"):
                locator = page.get_by_role(role, name=re.compile(pattern, re.I))
                for index in range(await locator.count() - 1, -1, -1):
                    candidate = locator.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
            locator = page.get_by_text(re.compile(pattern, re.I))
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
        return None

    async def inspect(self, receipt: SubmitReceipt) -> BrowserSnapshot:
        page = await self._conversation_page(receipt.conversation_url)
        main_text = await self._page_text(page)
        research_text = await self._research_frame_text(page)
        combined = "\n".join(value for value in (main_text, research_text) if value)
        count = await self._assistant_count(page)
        has_new = count > receipt.assistant_count_before
        result = await self._latest_result(page)
        if research_text:
            result = research_text
        state, user_action = classify_page_text(
            combined,
            has_new_output=has_new,
            has_research_output=bool(research_text),
            active_hint=await self._active_ui(page),
        )
        if is_transient_assistant_text(result):
            state = "RUNNING"
            result = None
        progress = None
        for line in combined.splitlines():
            if any(token in line.lower() for token in ("search", "research", "搜索", "研究", "引用", "source")):
                progress = line.strip()[:400]
        sources = await self._source_links(page)
        return BrowserSnapshot(
            state=state,
            conversation_url=page.url,
            progress=progress,
            user_action=user_action,
            result_text=result if state == "COMPLETE" else None,
            sources=sources,
            page_title=await page.title(),
        )

    async def _page_text(self, page: Page) -> str:
        for selector in ("main", "body"):
            try:
                text = await page.locator(selector).inner_text(timeout=5000)
                if text.strip():
                    return text
            except PlaywrightError:
                continue
        return ""

    async def _research_frame_text(self, page: Page) -> str:
        for frame in page.frames:
            if "internal://deep-research" in frame.url:
                try:
                    return (await frame.locator("body").inner_text(timeout=3000)).strip()
                except PlaywrightError:
                    return ""
        return ""

    async def _latest_result(self, page: Page) -> str | None:
        selectors = (
            '[data-message-author-role="assistant"]',
            "article[data-testid*='conversation-turn-assistant']",
        )
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            if count:
                try:
                    text = await locator.nth(count - 1).inner_text(timeout=3000)
                    return text.strip() or None
                except PlaywrightError:
                    continue
        return None

    async def _source_links(self, page: Page) -> list[str]:
        links: list[str] = []
        for frame in page.frames:
            try:
                hrefs = await frame.locator("a[href]").evaluate_all(
                    "els => els.map(e => e.href).filter(Boolean)"
                )
            except PlaywrightError:
                continue
            for href in hrefs:
                if (
                    isinstance(href, str)
                    and href.startswith(("http://", "https://"))
                    and "chatgpt.com" not in href
                    and "chat.openai.com" not in href
                    and href not in links
                ):
                    links.append(href)
        return links[:30]

    async def _active_ui(self, page: Page) -> bool:
        patterns = (
            re.compile(r"^(Stop generating|停止生成)$", re.I),
            re.compile(r"^(?:Pro\s*)?(Searching|Researching|Finalizing|Thinking|思考中|正在思考|正在搜索|正在研究|正在生成)\.?$", re.I),
        )
        for frame in page.frames:
            for pattern in patterns:
                for role in ("button", "status", "progressbar"):
                    locator = frame.get_by_role(role, name=pattern)
                    for index in range(await locator.count()):
                        if await locator.nth(index).is_visible():
                            return True
                locator = frame.get_by_text(pattern)
                for index in range(await locator.count()):
                    if await locator.nth(index).is_visible():
                        return True
        return False

    async def _assistant_count(self, page: Page) -> int:
        count = await page.locator('[data-message-author-role="assistant"]').count()
        if count:
            return count
        return await page.locator("article").count()

    async def _find_composer(self, page: Page) -> Locator:
        selectors = (
            "textarea[placeholder*='message' i]",
            "textarea[placeholder*='消息' i]",
            "textarea",
            "[contenteditable='true']",
        )
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
                except PlaywrightError:
                    continue
        role_locator = page.get_by_role("textbox")
        for index in range(await role_locator.count() - 1, -1, -1):
            candidate = role_locator.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                return candidate
        raise BrowserTaskError("找不到 ChatGPT 消息输入框；请检查专用浏览器页面或 UI 是否变化。")

    async def _click_send(self, page: Page) -> None:
        locator = page.locator("button[data-testid='send-button']")
        for index in range(await locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                await candidate.click(timeout=int(self.settings.browser_timeout_seconds * 1000))
                return
        locator = page.get_by_role(
            "button",
            name=re.compile(r"^(发送|发送提示词|Send|Send prompt|提交|Submit)$", re.I),
        )
        for index in range(await locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                await candidate.click(timeout=int(self.settings.browser_timeout_seconds * 1000))
                return
        raise BrowserTaskError("消息已填入，但找不到发送按钮。")

    async def _click_visible_text(self, page: Page, pattern: re.Pattern[str]) -> bool:
        for role in ("menuitem", "button", "option"):
            locator = page.get_by_role(role, name=pattern)
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click()
                    return True
        locator = page.get_by_text(pattern)
        for index in range(await locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                await candidate.click()
                return True
        return False

    async def _activate_research(self, page: Page) -> None:
        pattern = re.compile(r"^(深度研究|Deep research)$", re.I)
        if await self._click_visible_text(page, pattern):
            await page.wait_for_timeout(300)
            return
        plus = page.locator("button[data-testid='composer-plus-btn']")
        for index in range(await plus.count()):
            candidate = plus.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                await candidate.click()
                await page.wait_for_timeout(250)
                if await self._click_visible_text(page, pattern):
                    await page.wait_for_timeout(300)
                    return
                break
        # The menu may be behind the composer '+' / attachment control.
        for name in (r"添加文件等", r"Add", r"附件", r"Attach"):
            locator = page.get_by_role("button", name=re.compile(name, re.I))
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click()
                    await page.wait_for_timeout(250)
                    if await self._click_visible_text(page, pattern):
                        await page.wait_for_timeout(300)
                        return
        raise BrowserTaskError("当前 ChatGPT 页面没有找到 Deep Research 入口。")

    async def _start_research_plan_if_present(self, page: Page) -> None:
        pattern = re.compile(r"^(开始|Start|开始研究|Start research)$", re.I)
        await page.wait_for_timeout(500)
        await self._click_visible_text(page, pattern)

    async def _select_visible_model(self, page: Page, model: str) -> None:
        pattern = re.compile(re.escape(model), re.I)
        if await self._click_visible_text(page, pattern):
            return
        raise BrowserTaskError(f"当前页面找不到指定模型：{model}")
