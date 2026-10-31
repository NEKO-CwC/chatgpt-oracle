from __future__ import annotations

import asyncio
import hashlib
import math
import logging
import os
import re
import subprocess
import uuid
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
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
from .browser_resources import MAX_BROWSER_TABS, ProfileResources
from .submission_diagnostics import (
    console_probe_script, probe_event, probe_snapshot, read_console_probe_script,
    submission_identity, submission_phase, submission_send_attempted,
)
from .composer_drafts import COMPOSER_DRAFT_SCRIPT, ComposerPreparation
from .scheduler import _pid_alive
from .chatgpt_research import (
    NESTED_RESEARCH_CONTENT_PROBE, NESTED_RESEARCH_CONTROL, RESEARCH_ELEMENT_METADATA, RESEARCH_PANEL_SELECTOR,
    merge_research_surfaces, research_association,
)


class BrowserNeedsUserAction(RuntimeError):
    def __init__(
        self, reason: str, instructions: str, *,
        user_input_kind: str = "manual_browser", code: str | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.instructions = instructions
        self.user_input_kind = user_input_kind
        self.code = code


class BrowserComposerDraftPresent(BrowserNeedsUserAction):
    def __init__(self):
        super().__init__(
            "composer_draft_present",
            "ChatGPT 输入框已有未发送的草稿，服务已停止准备且未覆盖草稿。请先保存或处理该草稿，再明确发起新的提交。",
            user_input_kind="composer_draft", code="COMPOSER_DRAFT_PRESENT",
        )


class BrowserTaskError(RuntimeError):
    pass


class BrowserRuntimeRecoveryError(BrowserTaskError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrowserPageNotReady(BrowserTaskError):
    """Required page controls did not become usable within the readiness budget."""


class BrowserPreparationError(BrowserTaskError):
    """A task-owned draft could not be prepared; no human action is implied."""

    code = "COMPOSER_PREPARATION_FAILED"


class BrowserAccountPageUnavailable(BrowserPageNotReady):
    code = "ACCOUNT_PAGE_UNAVAILABLE"
    user_input_kind = "page_recovery"
    instructions = "ChatGPT 账户页面暂时无法加载。服务仅在发送前对自有、无草稿页面作有界刷新；未恢复时保留原身份稍后核查，不重提原问题。退出登录、登录或 CAPTCHA 由人工处理。"


class BrowserUnresponsiveError(BrowserTaskError):
    """A browser call exceeded its deadline; sending must never be retried."""


class BrowserCapacityError(BrowserTaskError):
    """The dedicated profile has no free physical tab; no page was created."""


def _serialize_preparation(method):
    @wraps(method)
    async def prepare(self, *args, **kwargs):
        # Four Sessions generate concurrently, but page hydration and input
        # preparation share one short operation queue. Avoid four simultaneous
        # cold SPA loads and competing chooser/menu/focus transactions.
        async with self._resources.ui_admitted():
            try:
                async with asyncio.timeout(3 * self.settings.browser_timeout_seconds):
                    return await method(self, *args, **kwargs)
            except TimeoutError as exc:
                self.mark_unresponsive()
                raise BrowserUnresponsiveError("Browser submission preparation exceeded its deadline; the request will not be resent.") from exc
    return prepare


@dataclass(frozen=True)
class StreamedAttachment:
    size: int
    mime_type: str | None
    content_disposition: str | None


def _is_explicit_login_page(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.hostname == "auth.openai.com":
        return True
    return parsed.path.rstrip("/").lower() in {"/auth/login", "/login", "/log-in", "/sign-in", "/auth/logout", "/logout", "/log-out", "/sign-out"}


_CHATGPT_VERIFICATION_PROBE = r"""() => {
    const turnSelector = '[data-message-author-role], [data-message-id], [data-content-search-unit-key], ' +
        '[data-chatgpt-selection-message-id], article[data-testid*="conversation-turn"]';
    const outsideTurn = node => !node.closest(turnSelector) && !node.querySelector(turnSelector)
        && !node.closest('blockquote, pre, code');
    const visible = node => {
        if (!node?.getClientRects().length || node.closest('[hidden], [aria-hidden="true"], [inert]')) return false;
        for (let ancestor = node; ancestor; ancestor = ancestor.parentElement) {
            const style = getComputedStyle(ancestor);
            if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse'
                    || Number(style.opacity) === 0) return false;
        }
        return true;
    };
    // The site displays this machine code in a toast/aside, without a CAPTCHA
    // iframe or the usual "Verify you are human" heading. Require both its
    // exact code and a visible site alert; conversation prose is not a blocker.
    const cloudflareAlert = [...document.querySelectorAll('[role="alert"]')].some(alert =>
        visible(alert) && outsideTurn(alert) && [alert, ...alert.querySelectorAll('div, p, span')].some(node =>
            visible(node) && outsideTurn(node) && (node.innerText || '').trim() === 'cloudflare_challenge'));
    const challenge = [...document.querySelectorAll('iframe[title*="captcha" i], iframe[src*="captcha" i], iframe[src*="challenges.cloudflare.com" i]')]
        .some(node => {
            const bounds = node.getBoundingClientRect();
            // Managed challenge/token probes can exist as zero/one-pixel
            // frames. Only a visibly interactive widget is a human handoff.
            return visible(node) && outsideTurn(node) && bounds.width >= 24 && bounds.height >= 24;
        }) ||
        [...document.querySelectorAll('h1, h2, p, div, span, main, section, aside, body')].some(node => visible(node) && outsideTurn(node) &&
            /^(?:Verify you are human|验证你是人类|请验证您是真人|人机验证)[.!。…]*$/i.test((node.innerText || '').trim()));
    // The SPA renders a standalone error card instead of the conversation
    // when its startup requests fail ("无法加载此 ChatGPT 对话 / 再试一次").
    // Require the exact heading inside the main region, outside any turn.
    const siteErrorLabels = new Set(['无法加载此 ChatGPT 对话', 'Failed to load conversation',
        'Failed to load ChatGPT conversation', 'Unable to load conversation']);
    const failureCard = [...document.querySelectorAll('main h1, main h2, main h3, main p, main div, main span')].some(node => {
        const text = (node.innerText || '').trim();
        if (!siteErrorLabels.has(text) || !visible(node) || !outsideTurn(node)) return false;
        if (node.querySelector('h1, h2, h3, p, div, span')) return false;  // innermost element only
        if (!node.closest('main')) return false;
        return [...node.closest('main').querySelectorAll('button, [role="button"]')].some(button =>
            visible(button) && /^(?:再试一次|重试|Try again|Retry)$/i.test((button.getAttribute('aria-label') || button.innerText || '').trim()));
    });
    const runtimeCode = cloudflareAlert ? 'cloudflare_challenge' : failureCard ? 'site_conversation_load_failed' : null;
    return {verification_required: challenge, site_alert_code: cloudflareAlert ? 'cloudflare_challenge' : null,
        runtime_error_code: runtimeCode, conversation_failure_visible: failureCard};
}"""


def _verification_instructions(surface: dict[str, Any]) -> str:
    return "请在专用浏览器中手动完成验证；服务不会绕过 CAPTCHA。"


def _is_chatgpt_download_host(host: str) -> bool:
    return host in {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
        "www.chat.openai.com",
        "oaiusercontent.com",
    } or host.endswith((".oaiusercontent.com", ".openai.com"))


def _matches_interpreter_filename(expected: str, actual: str) -> bool:
    if expected.casefold() == actual.casefold():
        return True
    # The current metadata endpoint may assign a collision suffix to the
    # stored download name while retaining the original sandbox_path route.
    stem, suffix = os.path.splitext(expected)
    return bool(re.fullmatch(re.escape(stem) + r"\([1-9]\d*\)" + re.escape(suffix), actual, re.I))


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
    # Counts are diagnostic only: ChatGPT virtualizes long conversation history.
    # The current turn is identified by the newly submitted user message and its
    # following assistant messages, never by the number of rendered old turns.
    user_message_ids_before: list[str] = field(default_factory=list)
    assistant_message_ids_before: list[str] = field(default_factory=list)
    submitted_prompt_sha256: str | None = None
    current_user_message_id: str | None = None
    current_assistant_message_id: str | None = None
    submission_state: str = "legacy_unverified"
    submission_verified_at: str | None = None
    research_expected: bool = False
    research_start_state: str | None = None
    research_user_message_id: str | None = None
    research_user_prompt_sha256: str | None = None
    research_assistant_message_id: str | None = None
    research_observed: bool = False
    research_state: str | None = None
    research_run_id: str | None = None
    research_report_id: str | None = None
    runtime_recovery_attempts: int = 0
    provisional_uncommitted_since: str | None = None
    account_index: int | None = None
    account_attempts: list[dict[str, Any]] = field(default_factory=list)
    account_switch: dict[str, Any] | None = None


@dataclass
class BrowserCapabilities:
    models: list[str]
    current_model: str | None
    reasoning_efforts: list[dict[str, str | int]]
    current_reasoning_effort: dict[str, str | int] | None
    quality_aliases: dict[str, str | None]
    url: str
    logged_in: bool
    account_index: int | None = None


@dataclass
class BrowserSnapshot:
    state: str
    conversation_url: str | None
    progress: str | None = None
    user_action: str | None = None
    result_text: str | None = None
    sources: list[str] = field(default_factory=list)
    page_title: str | None = None
    current_user_message_id: str | None = None
    current_assistant_message_id: str | None = None
    submission_state: str | None = None
    submission_verified_at: str | None = None
    generation_active: bool | None = None
    clarification_text: str | None = None
    user_input_kind: str | None = None
    runtime_error_code: str | None = None

    # Tool workflow evidence is separate from the outer assistant turn.
    research_observed: bool = False
    research_state: str | None = None
    research_plan_ready: bool = False
    research_report_complete: bool = False
    research_run_id: str | None = None
    research_report_id: str | None = None


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
    blocked = not has_new_output and (
        ("captcha" in lower)
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
    active = active_hint if active_hint is not None else any(
        phrase in lower
        for phrase in ("stop generating", "正在生成", "正在思考", "正在搜索", "正在研究")
    )
    if active or (not has_new_output and not has_research_output):
        return "RUNNING", None
    if _is_clarification_request(text):
        return "NEEDS_USER_INPUT", "ChatGPT 正在等待本轮问题的澄清或确认。"
    return "COMPLETE", None


def _is_clarification_request(text: str) -> bool:
    """Recognize an addressed request, rather than a mention of clarification.

    This only classifies content. The caller must separately prove the current
    turn and rule out active generation and browser-level blockers.
    """
    lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
        elif not in_code and not stripped.startswith(">"):
            lines.append(line)
    content = "\n".join(lines)
    boundary = r"(?:^|[\n.!?。！？:：,，]\s*)\s*(?:[-*]\s+|\d+[.)]\s+)?"
    requests = (
        r"please\s+(?:clarify|confirm|specify|provide|share|tell me|choose|select)\b",
        r"(?:could|can|would|will)\s+you\s+(?:please\s+)?(?:clarify|confirm|specify|provide|share|tell me|choose|select)\b",
        r"(?:i|we)\s+(?:need|require)\s+your\s+(?:input|confirmation|clarification|approval)\b",
        r"(?:chatgpt\s+)?needs your input\b",
        r"(?:请(?:先|你|您)?|烦请|能否|可否|可以请你|可以请您)(?:澄清|确认|补充|提供|说明|告知|选择|明确|告诉我)",
        r"(?:需要|等待)(?:你|您)的(?:输入|确认|澄清|补充|授权)",
    )
    if re.search(boundary + "(?:" + "|".join(requests) + ")", content, re.I):
        return True
    # Questions about the caller's goals or preferences remain readable; the
    # Main agent decides whether only the human can answer them.
    questions = (
        r"what\s+(?:is|are)\s+your\s+(?:goals?|priorit(?:y|ies)|preferences?|budget|deadline|intended audience)\b",
        r"(?:which|what)\b[^\n?？]{0,160}\b(?:do you prefer|would you prefer|matters (?:most|more) to you)\b",
        r"do you\s+(?:prefer|authorize|approve|consent)\b",
        r"(?:你|您)(?:的(?:目标|偏好|预算|截止时间)|希望|更(?:看重|倾向|希望)|是否(?:授权|同意|允许))",
    )
    return bool(re.search(boundary + "(?:" + "|".join(questions) + r")[^\n?？]*[?？]", content, re.I))


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


def _prompt_text_sha256(text: str) -> str:
    """Match the submitted text despite harmless rendered whitespace changes."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _resolve_current_turn(
    receipt: SubmitReceipt, messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve only a positively identified user turn and its assistant output.

    Virtualized history may expose a previously unseen old ID. An assistant ID
    absent from the baseline is therefore insufficient: it must follow the
    matching new user message, or have already been observed on that turn.
    """
    visible = [
        message for message in messages
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
        and isinstance(message.get("id"), str) and message["id"]
    ]
    user_index = next((
        index for index, message in enumerate(visible)
        if message["role"] == "user" and message["id"] == receipt.current_user_message_id
        and (
            not message["id"].startswith("fallback-turn-")
            or any(
                isinstance(text, str) and _prompt_text_sha256(text) == receipt.submitted_prompt_sha256
                for text in [message.get("prompt_text", message.get("text", "")), *message.get("prompt_text_candidates", [])]
            )
        )
    ), None)
    if user_index is None and not receipt.current_user_message_id and receipt.submitted_prompt_sha256:
        baseline_users = set(receipt.user_message_ids_before)
        candidates = [
            index for index, message in enumerate(visible)
            if message["role"] == "user" and message["id"] not in baseline_users
            and any(
                isinstance(text, str) and _prompt_text_sha256(text) == receipt.submitted_prompt_sha256
                for text in [message.get("prompt_text", message.get("text", "")), *message.get("prompt_text_candidates", [])]
            )
        ]
        # Repeated prompt text is ambiguous unless exactly one new matching
        # user turn is visible. Do not select an arbitrary matching old turn.
        if len(candidates) == 1:
            candidate = candidates[0]
            baseline_ids = set(receipt.user_message_ids_before + receipt.assistant_message_ids_before)
            anchors = [index for index, message in enumerate(visible) if message["id"] in baseline_ids]
            last_baseline_id = (receipt.assistant_message_ids_before or receipt.user_message_ids_before or [None])[-1]
            last_anchor_present = any(message["id"] == last_baseline_id for message in visible)
            if not baseline_ids or (last_anchor_present and anchors and candidate > max(anchors)):
                user_index = candidate
                receipt.current_user_message_id = visible[candidate]["id"]
                receipt.submission_state = "confirmed"
                receipt.submission_verified_at = receipt.submission_verified_at or utc_now()
    if user_index is not None:
        output: list[dict[str, Any]] = []
        baseline_assistants = set(receipt.assistant_message_ids_before)
        for message in visible[user_index + 1:]:
            if message["role"] == "user":
                break
            if message["role"] == "assistant" and message["id"] not in baseline_assistants:
                output.append(message)
        if output:
            receipt.current_assistant_message_id = output[-1]["id"]
        return output
    # The current user may leave the render window while a long answer grows.
    # A previously correlated assistant ID remains valid across that eviction.
    if receipt.current_user_message_id and receipt.current_assistant_message_id:
        assistant_index = next((
            index for index, message in enumerate(visible)
            if message["role"] == "assistant" and message["id"] == receipt.current_assistant_message_id
        ), None)
        if assistant_index is not None:
            return [visible[assistant_index]]
    return []


@dataclass
class _BrowserOperation:
    session_id: str | None
    conversation_url: str | None
    owned_pages: dict[Page, str] = field(default_factory=dict)
    preserved_pages: set[Page] = field(default_factory=set)
    primary_page: Page | None = None
    unresponsive: bool = False
    account_refreshes: int = 0
    send_attempted: bool = False
    runtime_recovery_attempts: set[int] = field(default_factory=set)


class ChatGPTBrowser:
    continuation_accepts_mode = True

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
        self._probe_targets: set[str] = set()
        self._resources = ProfileResources(settings.browser_profile_dir, lock_timeout_seconds=3 * settings.browser_timeout_seconds)
        self._start_lock = asyncio.Lock()
        self._driver_abort_lock = asyncio.Lock()
        self._legacy_operation = _BrowserOperation(None, None)
        self._operation: ContextVar[_BrowserOperation | None] = ContextVar("browser_operation", default=None)

    @property
    def _scope(self) -> _BrowserOperation:
        return self._operation.get() or self._legacy_operation

    @property
    def _owned_pages(self) -> dict[Page, str]:
        return self._scope.owned_pages

    @asynccontextmanager
    async def operation_scope(self, session_id: str | None = None, conversation_url: str | None = None):
        """Keep operation cleanup isolated while all Sessions share one CDP connection."""
        token = self._operation.set(_BrowserOperation(session_id, conversation_url))
        try:
            yield
        finally:
            self._operation.reset(token)

    @asynccontextmanager
    async def _ui_transaction(self, page: Page):
        async with self._resources.ui_locked():
            try:
                async with asyncio.timeout(2 * self.settings.browser_timeout_seconds):
                    await page.bring_to_front()
                    yield
            except TimeoutError as exc:
                self.mark_unresponsive()
                raise BrowserUnresponsiveError("ChatGPT UI interaction exceeded its deadline; delivery may be unknown, and the question will not be resubmitted.") from exc

    def mark_unresponsive(self) -> None:
        self._scope.unresponsive = True

    async def abort_driver(self) -> None:
        """Stop only our pinned Playwright 1.58 pipe subprocess, leaving Chrome.

        PipeTransport has no public force-stop API. Its owned subprocess and
        Connection.stop_async contract are verified in the pinned package.
        This is used only when cancellation failed to unwind a browser call.
        """
        async with self._driver_abort_lock:
            playwright = self._playwright
            if playwright is None:
                return
            process = playwright._impl_obj._connection._transport._proc
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
            await asyncio.wait_for(playwright.stop(), timeout=5)
            error = playwright._impl_obj._connection._transport.on_error_future
            if error.done() and not error.cancelled():
                outcome = error.exception()
                if outcome is not None:
                    logging.getLogger("chatgpt_oracle.browser").info("stopped browser driver outcome_type=%s", type(outcome).__name__)
            self._playwright = None
            self._browser = None
            self._context = None
            self._connected_cdp = False

    async def discard_unresponsive_pages(self) -> None:
        """Reclaim only this operation's targets through Chrome's local endpoint.

        The failed Playwright channel is deliberately not used for cleanup.
        Target destruction is verified before removing its ownership record.
        """
        targets = set(self._owned_pages.values())
        if not targets and self._scope.session_id is None:
            return
        if "" in targets:
            raise BrowserTaskError("Browser cleanup has an unconfirmed target identity; automatic close was not attempted.")
        parsed = urlparse(self.settings.browser_cdp_url or "")
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username:
            raise BrowserTaskError("Unresponsive browser cleanup requires its configured loopback HTTP CDP endpoint.")
        base = f"{parsed.scheme}://{parsed.netloc}"
        async with self._resources.ui_locked():
            async with self._resources.locked() as conn:
                if not targets:
                    targets = {row[0] for row in conn.execute("SELECT target_id FROM pages WHERE session_key=?", (self._scope.session_id,))}
                if not targets:
                    return
                registered = {row[0] for row in conn.execute("SELECT target_id FROM pages")}
                if not targets <= registered:
                    raise BrowserTaskError("Browser target ownership changed; automatic cleanup was not attempted.")
                async with httpx.AsyncClient(trust_env=False, timeout=min(5.0, self.settings.browser_timeout_seconds)) as client:
                    response = await client.get(base + "/json/list")
                    response.raise_for_status()
                    pages = {item["id"] for item in response.json() if item["type"] == "page"}
                    for target in targets & pages:
                        if len(pages) == 1:
                            response = await client.put(base + "/json/new?about:blank")
                            response.raise_for_status()
                            blank = response.json()
                            if blank.get("type") != "page" or blank.get("url") != "about:blank":
                                raise BrowserTaskError("Chrome did not confirm the idle blank target.")
                            pages.add(blank["id"])
                        response = await client.get(base + "/json/close/" + quote(target, safe=""))
                        response.raise_for_status()
                        pages.discard(target)
                    deadline = asyncio.get_running_loop().time() + min(5.0, self.settings.browser_timeout_seconds)
                    while True:
                        response = await client.get(base + "/json/list")
                        response.raise_for_status()
                        remaining = {item["id"] for item in response.json() if item["type"] == "page"} & targets
                        if not remaining:
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            raise BrowserTaskError("Chrome has not confirmed target destruction; ownership records were retained.")
                        await asyncio.sleep(0.05)
                conn.executemany("DELETE FROM pages WHERE target_id=?", [(target,) for target in targets])
                self._owned_pages.clear()
                self._scope.preserved_pages.clear()
                self._scope.primary_page = None

    async def start(self) -> None:
        async with self._start_lock:
            if self._context and (not self._connected_cdp or self._browser.is_connected()):
                return
            async with self._resources.locked():
                await self._start_locked()

    async def _start_locked(self) -> None:
        if self._context:
            await self._reset_connection()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
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
                await self._playwright.stop()
                self._playwright = None
                raise BrowserTaskError("专用 Chrome CDP 无法连接；不会另外启动浏览器 Profile。")
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
            await self._playwright.stop()
            self._playwright = None
            raise

    async def _connect_cdp(self) -> None:
        assert self._playwright is not None
        self._browser = await self._playwright.chromium.connect_over_cdp(self.settings.browser_cdp_url)
        contexts = self._browser.contexts
        if len(contexts) != 1:
            raise BrowserTaskError("专用 CDP 必须只有一个浏览器上下文。")
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
        args.append("about:blank")
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._launched_cdp_process = subprocess.Popen(
            [str(executable), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    async def _reset_connection(self) -> None:
        if self._browser and not self.settings.browser_cdp_url:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._connected_cdp = False

    async def close(self) -> None:
        await self.close_owned_pages()
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

    async def _target_id(self, page: Page) -> str:
        assert self._context is not None
        session = await self._context.new_cdp_session(page)
        try:
            return str((await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"])
        finally:
            await session.detach()

    async def _close_page_or_idle(self, page: Page) -> None:
        """Release a managed page without terminating the shared CDP Chrome.

        Call under the profile allocation lock. Native Chrome exits when its
        last tab closes; leave exactly that target as an unowned empty tab.
        """
        if self._connected_cdp and self._browser is not None:
            session = await self._browser.new_browser_cdp_session()
            try:
                targets = (await session.send("Target.getTargets"))["targetInfos"]
            finally:
                await session.detach()
            if sum(target["type"] == "page" for target in targets) == 1:
                if page.url != "about:blank":
                    await page.goto("about:blank", wait_until="domcontentloaded", timeout=int(self.settings.browser_timeout_seconds * 1000))
                return
        await page.close()

    async def _reap_orphan_pages(self, conn) -> None:
        assert self._context is not None
        rows = conn.execute("SELECT target_id,owner_pid,retained,session_key,session_primary FROM pages").fetchall()
        if not rows:
            return
        live = {await self._target_id(page): page for page in self._context.pages if not page.is_closed()}
        for target_id, owner_pid, retained, session_key, session_primary in rows:
            page = live.get(target_id)
            if page is None:
                conn.execute("DELETE FROM pages WHERE target_id=?", (target_id,))
            elif not retained and not _pid_alive(owner_pid):
                if session_key is not None and session_primary:
                    # A worker crash is not the end of a named Session. Keep
                    # its original target so recovery can inspect an in-flight
                    # answer without replaying the send or reloading the page.
                    conn.execute(
                        "UPDATE pages SET owner_pid=NULL,retained=1,retain_key=? WHERE target_id=?",
                        (session_key, target_id),
                    )
                else:
                    await self._close_page_or_idle(page)
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target_id,))

    async def _can_adopt_empty_home(self, page: Page) -> bool:
        parsed = urlparse(page.url)
        if (parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "chat.openai.com"}
                or parsed.path.rstrip("/") or parsed.query or parsed.fragment):
            return False
        try:
            return await page.evaluate(
                """() => {
                    const visible = node => node.getClientRects().length > 0;
                    const editors = [...document.querySelectorAll('textarea:not(#pending-home-input), [contenteditable="true"]')].filter(visible);
                    if (!editors.length) return false;
                    const hasDraft = editors.some(node =>
                        String(node.value ?? node.textContent ?? '').trim()
                        || node.querySelector('img,video,audio,canvas,svg,[contenteditable="false"]'));
                    const selectedFiles = [...document.querySelectorAll('input[type="file"]')].some(node => node.files?.length);
                    const attachmentControls = [...document.querySelectorAll('button,[role="button"]')].some(node =>
                        visible(node) && /Remove file|移除文件/i.test(node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || ''));
                    const pendingUpload = [...document.querySelectorAll('[role="progressbar"],[data-testid*="attachment"],[data-testid*="upload"]')].some(visible);
                    return !hasDraft && !selectedFiles && !attachmentControls && !pendingUpload;
                }"""
            ) is True
        except PlaywrightError:
            # A navigating or unready page has not proved safe to adopt.
            return False

    async def _new_page(self) -> Page:
        await self.start()
        assert self._context is not None
        async with self._resources.ui_locked():
            async with self._resources.locked() as conn:
                await self._reap_orphan_pages(conn)
                registered = {row[0] for row in conn.execute("SELECT target_id FROM pages")}
                # Reuse Chrome's idle tab or a verified empty login homepage so
                # either startup path leaves all four Session slots available.
                page = None
                for candidate in self._context.pages:
                    if candidate.is_closed():
                        continue
                    target_id = await self._target_id(candidate)
                    if target_id in registered:
                        continue
                    if candidate.url == "about:blank" or await self._can_adopt_empty_home(candidate):
                        page = candidate
                        break
                if page is None:
                    if self._browser is not None:
                        session = await self._browser.new_browser_cdp_session()
                        try:
                            targets = (await session.send("Target.getTargets"))["targetInfos"]
                            page_count = sum(target["type"] == "page" for target in targets)
                        finally:
                            await session.detach()
                    else:
                        page_count = len(self._context.pages)
                    if page_count >= MAX_BROWSER_TABS:
                        raise BrowserCapacityError("专用 Profile 已达到四个标签页上限；等待 Session 释放。")
                    existing = set(self._context.pages)
                    try:
                        page = await self._context.new_page()
                    except BaseException:
                        for created in set(self._context.pages) - existing:
                            self._owned_pages.setdefault(created, "")
                        raise
                self._owned_pages[page] = ""
                if self._scope.primary_page is None:
                    self._scope.primary_page = page
                target_id = await self._target_id(page)
                self._owned_pages[page] = target_id
                await self._install_console_probe(page, target_id)
                conn.execute(
                    "INSERT OR REPLACE INTO pages(target_id,owner_pid,retained,retain_key,session_key,session_primary) VALUES(?,?,0,?,?,?)",
                    (target_id, os.getpid(), self._scope.session_id, self._scope.session_id,
                     int(self._scope.session_id is not None and self._scope.primary_page is page)),
                )
                return page

    async def _install_console_probe(self, page: Page, target_id: str) -> None:
        """Keep page-side console/network failures for this owned target.

        Failure evidence used to disappear with the tab. The probe stores a
        bounded, secret-scrubbed ring in the page; the driver drains it into
        probe_snapshot() so an inspector or the finished job can read it.
        """
        if target_id in self._probe_targets:
            return
        try:
            await page.add_init_script(script=console_probe_script(target_id))
            self._probe_targets.add(target_id)
        except (PlaywrightError, AttributeError, TimeoutError):
            # A probe is evidence only; never block ownership on it.
            pass

    async def drain_page_probe(self, page: Page, *, reason: str) -> dict[str, list]:
        """Move page-side failure records into the process probe snapshot."""
        target_id = self._owned_pages.get(page)
        if not target_id:
            return {}
        try:
            value = await asyncio.wait_for(page.evaluate(read_console_probe_script()), timeout=2.0)
        except (PlaywrightError, TimeoutError, asyncio.CancelledError):
            return {}
        if not isinstance(value, dict):
            return {}
        identity = submission_identity()
        for item in value.get("errors") or []:
            if isinstance(item, dict):
                probe_event(target_id, f"page_console_{item.get('level', 'error')}", reason=reason,
                            offset_ms=item.get("t"), text=item.get("text"))
        for item in value.get("requests") or []:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status == "fetch_error" or (isinstance(status, int) and status >= 400):
                probe_event(target_id, "page_request_failed", reason=reason, offset_ms=item.get("t"),
                            status=status, url=item.get("url"), error=item.get("error"))
        return {**identity, **value}

    def page_probe_snapshot(self, page: Page) -> list[dict]:
        return probe_snapshot(self._owned_pages.get(page))

    async def _adopt_page(self, page: Page) -> None:
        if page in self._owned_pages:
            return
        async with self._resources.locked() as conn:
            target_id = await self._target_id(page)
            row = conn.execute("SELECT owner_pid,retained,retain_key,session_key FROM pages WHERE target_id=?", (target_id,)).fetchone()
            if row and row[3] is not None and row[3] != self._scope.session_id:
                raise BrowserCapacityError("目标页面属于另一个 Session；请等待该 Session 的串行操作队列。")
            if row and row[0] not in {None, os.getpid()} and _pid_alive(row[0]):
                raise BrowserCapacityError("目标页面正由另一个浏览器操作占用。")
            if row and row[1] and row[3] is not None:
                self._scope.preserved_pages.add(page)
            self._owned_pages[page] = target_id
            await self._install_console_probe(page, target_id)
            if self._scope.primary_page is None:
                self._scope.primary_page = page
            conn.execute(
                "INSERT OR REPLACE INTO pages(target_id,owner_pid,retained,retain_key,session_key,session_primary) VALUES(?,?,0,?,?,?)",
                (target_id, os.getpid(), self._scope.session_id, self._scope.session_id,
                 int(self._scope.session_id is not None and self._scope.primary_page is page)),
            )

    async def close_owned_pages(self, *, keep_for_user: bool = False, retain_key: str | None = None) -> None:
        if not self._owned_pages:
            return
        if self._scope.unresponsive:
            if keep_for_user:
                # Preserve the target ledger without using the failed driver.
                # Explicit Session close can reconnect and reclaim it later.
                async with self._resources.locked() as conn:
                    for target_id in self._owned_pages.values():
                        conn.execute(
                            "UPDATE pages SET owner_pid=NULL,retained=1,retain_key=? WHERE target_id=? AND owner_pid=?",
                            (retain_key or self._scope.session_id, target_id, os.getpid()),
                        )
                self._owned_pages.clear()
                self._scope.preserved_pages.clear()
                self._scope.primary_page = None
                return
            await self.discard_unresponsive_pages()
            return
        async with self._resources.ui_locked():
            async with self._resources.locked() as conn:
                for page, target_id in list(self._owned_pages.items()):
                    keep_primary = keep_for_user and page is self._scope.primary_page
                    if (keep_primary or page in self._scope.preserved_pages) and not page.is_closed():
                        if target_id:
                            conn.execute(
                                "UPDATE pages SET owner_pid=NULL,retained=1,retain_key=? WHERE target_id=?",
                                (retain_key or self._scope.session_id, target_id),
                            )
                    else:
                        if not page.is_closed():
                            await self._close_page_or_idle(page)
                        if target_id:
                            conn.execute("DELETE FROM pages WHERE target_id=?", (target_id,))
                    self._owned_pages.pop(page, None)
                self._scope.preserved_pages.clear()
                self._scope.primary_page = None

    async def has_session_page(self, session_id: str) -> bool:
        """Check recorded ownership without borrowing another Session's page."""
        async with self._resources.locked() as conn:
            return conn.execute(
                "SELECT 1 FROM pages WHERE session_key=? LIMIT 1", (session_id,),
            ).fetchone() is not None

    async def _session_page(self) -> Page | None:
        session_id = self._scope.session_id
        if session_id is None:
            return None
        primary = self._scope.primary_page
        if primary is not None and not primary.is_closed():
            return primary
        assert self._context is not None
        async with self._resources.locked() as conn:
            targets = {row[0] for row in conn.execute(
                "SELECT target_id FROM pages WHERE session_key=? AND session_primary=1", (session_id,),
            )}
        for page in self._context.pages:
            if not page.is_closed() and await self._target_id(page) in targets:
                await self._adopt_page(page)
                return page
        return None

    async def _page(self, *, navigate: bool = True) -> Page:
        await self.start()
        assert self._context is not None
        if self._scope.session_id is not None:
            page = await self._session_page()
            if page is not None:
                return page
            if self._scope.conversation_url:
                return await self._conversation_page(self._scope.conversation_url)
        else:
            # Unscoped diagnostics may use an unmanaged login/home page, but
            # never a retained Session or an unrelated existing conversation.
            async with self._resources.locked() as conn:
                registered = {row[0] for row in conn.execute("SELECT target_id FROM pages")}
            for candidate in self._context.pages:
                parsed = urlparse(candidate.url)
                if parsed.hostname not in {"chatgpt.com", "chat.openai.com"} or parsed.path.rstrip("/"):
                    continue
                if candidate in self._owned_pages or await self._target_id(candidate) not in registered:
                    return candidate
        page = await self._new_page()
        if navigate:
            try:
                await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=int(self.settings.browser_timeout_seconds * 1000))
            except PlaywrightError:
                # A partially loaded SPA remains inspectable for manual login.
                pass
        return page

    async def _conversation_page(self, conversation_url: str, *, create: bool = True) -> Page:
        """Reuse the exact target without navigation or foreground focus."""
        await self.start()
        assert self._context is not None
        target = conversation_url.rstrip("/")
        scoped = await self._session_page()
        if scoped is not None and scoped.url.rstrip("/") == target:
            return scoped
        for page in self._context.pages:
            if not page.is_closed() and page.url.rstrip("/") == target:
                await self._adopt_page(page)
                return page
        if not create:
            raise BrowserTaskError(f"ChatGPT conversation tab is not open: {conversation_url}")
        if scoped is not None:
            raise BrowserTaskError("Session 的已保留页面与目标会话不一致；未导航或修改原页面。")
        page = await self._new_page()
        try:
            await page.goto(
                conversation_url,
                wait_until="domcontentloaded",
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        except PlaywrightError:
            pass
        return page

    async def _fresh_conversation_page(self) -> Page:
        await self.start()
        assert self._context is not None
        existing = await self._session_page()
        if existing is not None:
            if existing.url.rstrip("/") not in {"https://chatgpt.com", "about:blank"}:
                raise BrowserTaskError("Session 已绑定现有会话；请使用继续对话操作。")
            await self._check_composer_draft(existing, required=False)
            page = existing
            if page.url != "about:blank":
                # A retained home page may contain attachments even with no
                # text draft. Observe it in place; only a new owned empty page
                # is eligible for the bounded recovery below.
                return page
        else:
            page = await self._new_page()
        try:
            await page.goto(
                "https://chatgpt.com/",
                wait_until="domcontentloaded",
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        except PlaywrightError:
            pass
        return page

    async def _auxiliary_conversation_page(self, conversation_url: str) -> tuple[Page, bool]:
        """Borrow only the matching resident tab; otherwise allocate within the cap.

        The boolean says whether the page needs initial navigation. A new named
        primary remains owned until the outer Session cleanup decides retention.
        Borrowing never reloads, navigates, or clears a resident composer draft.
        """
        await self.start()
        assert self._context is not None
        if self._scope.session_id is None:
            # Legacy unscoped exports keep their temporary-page contract. At
            # capacity they wait; a caller cannot borrow a named Session lane.
            return await self._new_page(), True
        await self._session_page()
        for page in self._context.pages:
            if page.is_closed() or page.url.rstrip("/") != conversation_url.rstrip("/"):
                continue
            async with self._resources.locked() as conn:
                target_id = await self._target_id(page)
                row = conn.execute("SELECT session_key FROM pages WHERE target_id=?", (target_id,)).fetchone()
            if row and row[0] is not None and row[0] != self._scope.session_id:
                raise BrowserCapacityError("目标附件或导出的页面属于另一个 Session；必须先进入该 Session 的队列。")
            await self._adopt_page(page)
            if row:
                self._scope.preserved_pages.add(page)
            return page, False
        return await self._new_page(), True

    async def _close_auxiliary_page(self, page: Page, temporary: bool) -> None:
        if not temporary:
            return
        if self._scope.session_id is not None and self._scope.primary_page is page:
            return
        async with self._resources.ui_locked():
            async with self._resources.locked() as conn:
                target_id = self._owned_pages.pop(page, None)
                await self._close_page_or_idle(page)
                if target_id:
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target_id,))
                if self._scope.primary_page is page:
                    self._scope.primary_page = None

    async def _resident_conversation_request(self, page: Page, conversation_id: str) -> str | None:
        """Replay only a route already observed in this page's resource history."""
        return await page.evaluate(
            """(id) => performance.getEntriesByType('resource').map(item => item.name).reverse()
                .find(value => { const url = new URL(value);
                    return url.origin === location.origin
                        && url.pathname === '/backend-api/conversations/' + id
                        && !url.searchParams.has('before') && !url.searchParams.has('after');
                }) || null""",
            conversation_id,
        )

    async def _conversation_request_headers(self, page: Page, conversation_url: str, *, navigate: bool) -> dict[str, str]:
        conversation_id = urlparse(conversation_url).path.rstrip("/").rsplit("/", 1)[-1]
        conversation_path = f"/backend-api/conversations/{conversation_id}"
        observed_request = None if navigate else await self._resident_conversation_request(page, conversation_id)
        if not navigate and not observed_request:
            # The attachment URL itself came from the verified export. The
            # existing metadata helper refreshes authentication on 401/403.
            return {}
        try:
            async with page.expect_response(
                lambda response: urlparse(response.url).path == conversation_path,
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            ) as response_info:
                if navigate:
                    await page.goto(
                        conversation_url, wait_until="domcontentloaded",
                        timeout=int(self.settings.browser_timeout_seconds * 1000),
                    )
                else:
                    await _authenticated_page_json_get(
                        page, observed_request, _client_api_headers({}, conversation_path),
                    )
            conversation_response = await response_info.value
            if conversation_response.status != 200:
                raise BrowserTaskError("Could not refresh the logged-in conversation context")
            return await conversation_response.request.all_headers()
        except PlaywrightError as exc:
            raise BrowserTaskError("Could not refresh the logged-in conversation context") from exc

    async def has_conversation_tab(self, conversation_url: str) -> bool:
        target = conversation_url.rstrip("/")
        if self._context is None:
            # A restarted broker has no driver yet; its retained Chrome pages
            # can still be alive. Observe CDP without launching or navigating.
            if self.settings.browser_cdp_url:
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=min(3.0, self.settings.browser_timeout_seconds)) as client:
                        response = await client.get(self.settings.browser_cdp_url.rstrip("/") + "/json/list")
                        response.raise_for_status()
                        return any(item.get("type") == "page" and str(item.get("url", "")).rstrip("/") == target for item in response.json())
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    return False
            return False
        return any(page.url.rstrip("/") == target for page in self._context.pages)

    async def close_conversation_tabs(self, conversation_url: str) -> bool:
        if self._context is None:
            return False
        target = conversation_url.rstrip("/")
        closed = False
        async with self._resources.ui_locked():
            async with self._resources.locked() as conn:
                for page in list(self._context.pages):
                    if page.url.rstrip("/") != target:
                        continue
                    target_id = self._owned_pages.get(page) or await self._target_id(page)
                    row = conn.execute("SELECT session_key FROM pages WHERE target_id=?", (target_id,)).fetchone()
                    if self._scope.session_id is not None and row and row[0] not in {None, self._scope.session_id}:
                        continue
                    await self._close_page_or_idle(page)
                    self._owned_pages.pop(page, None)
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target_id,))
                    closed = True
            return closed

    async def close_retained_pages(self, retain_key: str) -> None:
        await self.start()
        assert self._context is not None
        async with self._resources.ui_locked():
            async with self._resources.locked() as conn:
                targets = {row[0] for row in conn.execute("SELECT target_id FROM pages WHERE retained=1 AND retain_key=?", (retain_key,))}
                for page in list(self._context.pages):
                    target_id = await self._target_id(page)
                    if target_id in targets:
                        await self._close_page_or_idle(page)
                        conn.execute("DELETE FROM pages WHERE target_id=?", (target_id,))

    async def browser_status(self) -> dict[str, Any]:
        page = await self._page()
        manual = None
        try:
            await self._check_account_page_error(page)
        except (BrowserNeedsUserAction, BrowserAccountPageUnavailable) as exc:
            manual = exc
        requires_login = await self._page_requires_login(page)
        logged_in = manual is None and not requires_login and await self._page_has_composer(page)
        if manual is None and not requires_login and not logged_in:
            try:
                await self._require_composer(page)
                logged_in = True
            except BrowserNeedsUserAction:
                requires_login = True
            except BrowserTaskError:
                # A cold diagnostic tab may not hydrate before the configured
                # deadline. Report unready, without claiming an expired login.
                logged_in = False
        return {
            "ready": logged_in,
            "logged_in": logged_in,
            "url": page.url,
            "title": await page.title(),
            "profile_dir": str(self.settings.browser_profile_dir),
            "headless": self.settings.browser_headless,
            "cdp_url": self.settings.browser_cdp_url,
            "message": (
                manual.instructions if manual else None if logged_in else "请在专用浏览器 Profile 中完成 ChatGPT 登录。"
                if requires_login else "ChatGPT 页面尚未就绪，未检测到可用的消息输入框。"
            ),
            **({"error_code": manual.code, "user_input_kind": manual.user_input_kind} if manual else {}),
        }

    async def _page_has_composer(self, page: Page) -> bool:
        if "chatgpt.com" not in page.url and "chat.openai.com" not in page.url:
            return False
        if "auth.openai.com" in page.url:
            return False
        composer_candidates = page.locator("textarea:not(#pending-home-input), [contenteditable='true']")
        for index in range(await composer_candidates.count()):
            if await composer_candidates.nth(index).is_visible():
                return True
        return False

    async def _page_requires_login(self, page: Page) -> bool:
        if _is_explicit_login_page(page.url):
            return True
        login_fields = page.locator("input[type='email']:visible, input[name='username']:visible")
        if await login_fields.count():
            return True
        # The signed-out home page also has a working composer. Its explicit
        # account controls take precedence over composer presence.
        pattern = re.compile(r"^(登录|登入|免费注册|Log in|Sign in|Sign up(?: for free)?)$", re.I)
        for role in ("button", "link"):
            controls = page.get_by_role(role, name=pattern)
            for index in range(await controls.count()):
                if await controls.nth(index).is_visible():
                    return True
        return False

    async def _require_composer(self, page: Page, *, conversation_url: str | None = None) -> None:
        # Navigation recovery belongs only to fresh submission preparation.
        # This helper is also called while inspecting existing conversations,
        # whose draft, attachments and in-flight answer must remain untouched.
        await self._require_composer_once(page)

    async def _require_composer_once(self, page: Page) -> None:
        await self._check_verification_challenge(page)
        await self._check_account_page_error(page)
        if await self._page_requires_login(page):
            raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。")
        # A fresh SPA tab may render after DOMContentLoaded. Wait for the
        # actual composer, then recheck authentication on this exact tab.
        try:
            await page.locator("textarea:not(#pending-home-input):visible, [contenteditable='true']:visible").first.wait_for(
                state="visible", timeout=int(self.settings.browser_timeout_seconds * 1000)
            )
        except PlaywrightError as exc:
            await self._check_account_page_error(page)
            if await self._page_requires_login(page):
                raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。") from exc
            raise BrowserPageNotReady("ChatGPT 页面尚未就绪，找不到消息输入框。") from exc
        await self._check_account_page_error(page)
        if await self._page_requires_login(page):
            raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。")

    async def _check_account_page_error(self, page: Page) -> None:
        if urlparse(page.url).path.rstrip("/"):
            return
        error = page.get_by_text(re.compile(r"^(?:无法加载你的账户|无法加载您的账户|Unable to load your account|Could(?: not|n't) load your account)$", re.I))
        for index in range(await error.count()):
            if await error.nth(index).is_visible():
                raise BrowserAccountPageUnavailable(BrowserAccountPageUnavailable.instructions)

    async def _intelligence_trigger(self, page: Page) -> Locator:
        semantic = page.get_by_role(
            "button", name=re.compile(r"^(选择 ChatGPT 模型|Choose ChatGPT model|Select ChatGPT model)$", re.I)
        )
        groups = (
            semantic.filter(visible=True),
            page.locator('[data-composer-body] button.__composer-pill').filter(visible=True),
            page.locator('[data-composer-body] button[aria-haspopup="menu"]').filter(visible=True),
        )
        deadline = asyncio.get_running_loop().time() + self.settings.browser_timeout_seconds
        while True:
            for group_index, candidates in enumerate(groups):
                indices = range(await candidates.count())
                for index in indices if group_index == 0 else reversed(indices):
                    candidate = candidates.nth(index)
                    if await candidate.is_enabled():
                        return candidate
            if await self._page_requires_login(page):
                raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。")
            await self._check_verification_challenge(page)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BrowserPageNotReady(
                    f"ChatGPT 模型/能力选择器未就绪：等待 {self.settings.browser_timeout_seconds:g} 秒后仍无可见且可用的控件。"
                )
            await asyncio.sleep(min(0.1, remaining))

    async def _check_verification_challenge(self, page: Page) -> None:
        surface = await page.evaluate(_CHATGPT_VERIFICATION_PROBE)
        if isinstance(surface, dict) and surface.get("verification_required"):
            raise BrowserNeedsUserAction("verification_required", _verification_instructions(surface))
        if isinstance(surface, dict) and surface.get("runtime_error_code"):
            raise BrowserPageNotReady("ChatGPT 站点出现 cloudflare_challenge 运行时告警；发送前仅恢复空闲自有新页。")

    async def _can_recover_submission_page(self, page: Page) -> bool:
        """Prove an owned, pre-send new page is empty before changing it."""
        if (self._scope.send_attempted or page not in self._owned_pages
                or page in self._scope.preserved_pages):
            return False
        parsed = urlparse(page.url)
        if page.url not in {"about:blank", "chrome-error://chromewebdata/", "chrome-error://chromewebdata"} and not (
            parsed.scheme == "https" and parsed.hostname in {"chatgpt.com", "chat.openai.com"}
            and not parsed.path.rstrip("/") and not parsed.query and not parsed.fragment
        ):
            return False
        if await self._page_requires_login(page):
            raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。")
        try:
            await self._check_verification_challenge(page)
        except BrowserPageNotReady:
            # A runtime toast is exactly the state the empty-page recovery
            # handles. Real CAPTCHA/login still raises BrowserNeedsUserAction.
            pass
        await self._check_composer_draft(page, required=False)
        try:
            return await page.evaluate(
                """() => {
                    const visible = node => node.getClientRects().length > 0;
                    const editors = [...document.querySelectorAll('textarea, [contenteditable="true"]')];
                    const hasDraft = editors.some(node => String(node.value ?? node.textContent ?? '').trim()
                        || node.querySelector('img,video,audio,canvas,svg,[contenteditable="false"]'));
                    const selectedFiles = [...document.querySelectorAll('input[type="file"]')].some(node => node.files?.length);
                    const attachments = [...document.querySelectorAll('button,[role="button"]')].some(node =>
                        visible(node) && /Remove(?: file)?|移除(?:文件)?/i.test(node.getAttribute('aria-label') || node.getAttribute('title') || ''));
                    const pendingUpload = [...document.querySelectorAll('[data-testid*="attachment"],[data-testid*="upload"],[data-upload-id],[data-composer-body] [role="progressbar"]')].some(visible);
                    const existingTurns = [...document.querySelectorAll('[data-message-author-role],[data-content-search-unit-key],[data-chatgpt-selection-message-id]')].some(visible);
                    const generating = [...document.querySelectorAll('button,[role="button"]')].some(node => visible(node) &&
                        (node.getAttribute('data-testid') === 'stop-button' || /^(?:Stop generating|停止生成|Stop|停止|Stop research|停止研究)$/i.test(node.getAttribute('aria-label') || node.textContent || '')));
                    return !hasDraft && !selectedFiles && !attachments && !pendingUpload && !existingTurns && !generating;
                }"""
            ) is True
        except PlaywrightError:
            return False

    async def _rebuild_submission_page(self, page: Page, *, timeout_ms: int) -> Page:
        """Replace only the proven empty owned page, without exceeding the cap."""
        async with self._resources.ui_locked():
            if not await self._can_recover_submission_page(page):
                raise BrowserPageNotReady("ChatGPT 页面未证明为空闲自有新页；未关闭或重建页面。")
            async with self._resources.locked() as conn:
                target_id = self._owned_pages.get(page)
                # Close before allocating: a full four-page profile must never
                # grow a fifth page. The sole Chrome page becomes about:blank.
                await self._close_page_or_idle(page)
                if target_id:
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target_id,))
                self._owned_pages.pop(page, None)
                if self._scope.primary_page is page:
                    self._scope.primary_page = None
            replacement = await self._new_page()
            try:
                await replacement.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightError:
                # The next readiness check observes the replacement directly.
                pass
            return replacement

    async def _prepare_submission_page(self, page: Page, *, needs_selection: bool, can_reload: bool) -> Page:
        reloads = [0]
        try:
            async with asyncio.timeout(self.settings.browser_timeout_seconds):
                return await self._prepare_submission_page_within_budget(page, needs_selection=needs_selection, can_reload=can_reload, reloads=reloads)
        except TimeoutError as exc:
            self.mark_unresponsive()
            raise BrowserPageNotReady(
                f"ChatGPT 新页在 {self.settings.browser_timeout_seconds:g} 秒的总就绪预算内未完成检查（含错误诊断）；"
                f"页面重新加载 {reloads[0]} 次，尚未上传或发送。"
            ) from exc

    async def _prepare_submission_page_within_budget(self, page: Page, *, needs_selection: bool, can_reload: bool, reloads: list[int]) -> Page:
        # A fresh SPA can replace an already visible composer while hydrating.
        # Readiness includes both controls and has one total, configured budget.
        deadline = asyncio.get_running_loop().time() + self.settings.browser_timeout_seconds
        recoverable = (can_reload and page in self._owned_pages
                       and page not in self._scope.preserved_pages and not self._scope.send_attempted)
        attempts = 3 if recoverable else 1
        for attempt in range(attempts):
            remaining = deadline - asyncio.get_running_loop().time()
            budget = max(0.01, remaining / (attempts - attempt))
            if attempt and not await self._can_recover_submission_page(page):
                raise BrowserPageNotReady("ChatGPT 页面未证明为空闲自有新页；未刷新、关闭或重建页面。")
            try:
                async with asyncio.timeout(budget):
                    if attempt:
                        if attempt == 1:
                            reloads[0] += 1
                            submission_phase("page_recovery_reload")
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=max(1, int(budget * 1000)))
                            except PlaywrightError:
                                pass
                        else:
                            submission_phase("page_recovery_rebuild")
                            page = await self._rebuild_submission_page(page, timeout_ms=max(1, int(budget * 1000)))
                    await self._check_verification_challenge(page)
                    await self._require_composer(page)
                    await self._check_composer_draft(page)
                    if needs_selection:
                        await self._intelligence_trigger(page)
                    # Ensure the composer was not removed during picker loading.
                    if not await self._page_has_composer(page):
                        raise BrowserPageNotReady("模型控件加载期间消息输入框被页面替换。")
                return page
            except (TimeoutError, BrowserPageNotReady, PlaywrightError) as exc:
                if await self._page_requires_login(page):
                    raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。") from exc
                try:
                    await self._check_verification_challenge(page)
                except BrowserPageNotReady:
                    pass
                if attempt + 1 == attempts:
                    raise BrowserPageNotReady(
                        f"ChatGPT 新页在 {self.settings.browser_timeout_seconds:g} 秒的就绪预算内未完成输入框和模型控件加载；"
                        f"页面重新加载 {reloads[0]} 次，尚未上传或发送。"
                    ) from exc
                logging.getLogger("chatgpt_oracle.browser").info(
                    "fresh submission page not ready; bounded pre-send recovery stage=%d exception_type=%s", attempt + 1, type(exc).__name__
                )

    def _intelligence_menu(self, page: Page) -> Locator:
        return page.locator(
            '[data-testid="composer-intelligence-picker-content"], [role="menu"]'
        ).filter(has=page.locator('[role="slider"], [role="menuitemradio"]')).filter(visible=True).first

    async def _open_intelligence_menu(self, page: Page) -> Locator:
        menu = self._intelligence_menu(page)
        for attempt in range(2):
            await self._close_intelligence_menu(page)
            try:
                trigger = await self._intelligence_trigger(page)
                await trigger.wait_for(state="visible", timeout=5000)
                if not await trigger.is_enabled():
                    raise BrowserTaskError("ChatGPT 模型/能力选择器当前不可用。")
                await trigger.click()
                await menu.wait_for(state="visible", timeout=5000)
                slider = menu.locator('[role="slider"]')
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
        menu = self._intelligence_menu(page)
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

    @classmethod
    def _effort_matches(cls, expected: str, actual: str) -> bool:
        expected_label = cls._normalize_label(expected)
        actual_label = cls._normalize_label(actual)
        for name, aliases in cls._QUALITY_ALIASES.items():
            names = aliases | {name}
            if expected_label in names:
                return actual_label in names
        # These are discrete choices: High must never match Extra High.
        return expected_label == actual_label

    @classmethod
    def _requested_effort_matches(cls, requested: str, actual: str, *, allow_downgrade: bool) -> bool:
        return cls._effort_matches(requested, actual) or (
            allow_downgrade
            and cls._effort_matches("pro", requested)
            and cls._effort_matches("extra_high", actual)
        )

    async def _effort_control(self, menu: Locator) -> Locator:
        control = menu.get_by_role("menuitem", name=re.compile(r"^(能力|强度|reasoning|effort|intensity|power)$", re.I))
        if await control.count():
            return control.first
        raise BrowserTaskError("当前 ChatGPT 页面没有找到能力滑块。")

    async def _effort_label(self, menu: Locator) -> str:
        try:
            text = await menu.inner_text(timeout=int(min(3.0, self.settings.browser_timeout_seconds) * 1000))
        except PlaywrightError as exc:
            raise BrowserPageNotReady("ChatGPT 能力菜单在有界读取期间消失或未就绪；尚未发送。") from exc
        lines = [line.strip() for line in text.splitlines() if line.strip()]
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
        minimum, maximum, original = await self._effort_range(menu)
        efforts: list[dict[str, str | int]] = []
        # This custom Radix slider responds reliably to arrow keys; Home is
        # ignored when the current value is already at the maximum.
        try:
            await self._set_effort_value(menu, minimum)
            for value in range(minimum, maximum + 1):
                if value != minimum:
                    await self._press_effort_key(menu, "ArrowRight")
                _, _, current = await self._effort_range(menu)
                if current != value:
                    raise BrowserTaskError("ChatGPT 能力滑块未响应，不能确认可用档位。")
                efforts.append({"value": current, "label": await self._effort_label(menu)})
        finally:
            if restore:
                await self._set_effort_value(menu, original)
        return efforts, original

    async def _effort_range(self, menu: Locator) -> tuple[int, int, int]:
        slider = menu.locator('[role="slider"]')
        try:
            # Read one rendered control atomically; separate attribute reads
            # can span a React replacement and wait the global 30s timeout.
            values = await slider.evaluate(
                "node => ['aria-valuemin','aria-valuemax','aria-valuenow'].map(name => node.getAttribute(name))",
                timeout=int(min(3.0, self.settings.browser_timeout_seconds) * 1000),
            )
            minimum, maximum, current = [
                int(value) for value in values
            ]
        except PlaywrightError as exc:
            raise BrowserPageNotReady("ChatGPT 能力控件在有界读取期间消失或未就绪；尚未发送。") from exc
        except (TypeError, ValueError) as exc:
            raise BrowserTaskError("ChatGPT 能力滑块缺少有效范围或当前值。") from exc
        if not minimum <= current <= maximum:
            raise BrowserTaskError("ChatGPT 能力滑块范围无效。")
        return minimum, maximum, current

    async def _set_effort_value(self, menu: Locator, target: int) -> None:
        minimum, maximum, current = await self._effort_range(menu)
        if not minimum <= target <= maximum:
            raise BrowserTaskError("请求的能力值超出当前页面范围。")
        for _ in range(maximum - minimum + 1):
            if current == target:
                return
            await self._press_effort_key(menu, "ArrowRight" if current < target else "ArrowLeft")
            _, _, updated = await self._effort_range(menu)
            if updated == current:
                raise BrowserTaskError("ChatGPT 能力滑块未响应键盘操作。")
            current = updated
        raise BrowserTaskError("ChatGPT 能力滑块未到达请求值。")

    async def _model_view(self, menu: Locator) -> Locator:
        radios = menu.locator('[role="menuitemradio"]')
        visible_count = 0
        for index in range(await radios.count()):
            if await radios.nth(index).is_visible():
                visible_count += 1
        if visible_count == 0:
            toggle = menu.get_by_role("menuitem", name=re.compile(r"选择模型|select model", re.I))
            if await toggle.count():
                await toggle.focus(timeout=2000)
                await toggle.press("Enter", timeout=2000)
                await asyncio.sleep(0.1)
        return menu.locator('[role="menuitemradio"]').filter(visible=True)

    async def _activate_model_radio(self, radio: Locator) -> None:
        # The model/effort view is animated under the pointer. Its outgoing
        # data-effort-only label can cover the incoming model radio. Keyboard
        # activation follows the menu's own accessibility interaction and
        # does not click through that surface or mutate the DOM.
        if await radio.get_attribute("aria-checked") == "true":
            return
        await radio.focus(timeout=2000)
        await radio.press("Enter", timeout=2000)

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
        await self._require_composer(page)
        async with self._ui_transaction(page):
            menu = await self._open_intelligence_menu(page)
            try:
                efforts, original = await self._discover_efforts(menu)
                models, current_model = await self._read_model_options(menu)
            finally:
                await self._close_intelligence_menu(page)
        aliases: dict[str, str | None] = {"extra_high": None, "pro": None}
        for item in efforts:
            label = str(item["label"])
            normalized = self._normalize_label(label)
            for quality, names in self._QUALITY_ALIASES.items():
                if normalized in names:
                    aliases[quality] = label
        current_effort = next((item for item in efforts if int(item["value"]) == original), None)
        return BrowserCapabilities(
            models=models,
            current_model=current_model,
            reasoning_efforts=efforts,
            current_reasoning_effort=current_effort,
            quality_aliases=aliases,
            url=page.url,
            logged_in=True,
        )

    async def _select_effort(
        self,
        page: Page,
        target: str,
        *,
        allow_downgrade: bool = False,
    ) -> tuple[str, int, str]:
        menu = await self._open_intelligence_menu(page)
        target_normalized = self._normalize_label(target)

        def matches(label: str) -> bool:
            return self._effort_matches(target, label)

        actual = await self._effort_label(menu)
        if matches(actual):
            # A verified current target needs no tour through Instant/Pro.
            # The final pre-send check still rechecks this selection later.
            _, _, current = await self._effort_range(menu)
            await self._close_intelligence_menu(page)
            return actual, current, "explicit"
        efforts, _ = await self._discover_efforts(menu)

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
        await self._set_effort_value(menu, int(selected["value"]))
        actual = await self._effort_label(menu)
        if (not self._effort_matches(str(selected["label"]), actual)
                or not self._requested_effort_matches(target, actual, allow_downgrade=allow_downgrade)):
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
                await self._activate_model_radio(radio)
                await self._close_intelligence_menu(page)
                verification_menu = await self._open_intelligence_menu(page)
                try:
                    _, actual = await self._read_model_options(verification_menu)
                    if actual != text:
                        raise BrowserTaskError(f"模型选择未验证：请求 {text}，页面显示 {actual}")
                    return actual
                finally:
                    await self._close_intelligence_menu(page)
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
    ) -> dict[str, Any]:
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
            if not self._requested_effort_matches(target_effort, resolved_effort, allow_downgrade=allow_downgrade):
                raise BrowserTaskError(f"能力选择未验证：请求 {target_effort}，页面显示 {resolved_effort}；尚未发送。")
            source = effort_source if not resolved_model else f"{source}+{effort_source}"
        return {
            "requested_reasoning_effort": target_effort,
            "allow_downgrade": allow_downgrade,
            "resolved_model": resolved_model,
            "resolved_reasoning_effort": resolved_effort,
            "selection_source": source,
            "selection_verified_at": utc_now(),
        }

    async def _verify_selection_after_upload(self, page: Page, selection: dict[str, Any]) -> None:
        """Recheck original intent after attachments and draft preparation."""
        expected = selection["resolved_reasoning_effort"]
        if expected is None and selection["resolved_model"] is None:
            return
        menu = await self._open_intelligence_menu(page)
        try:
            if expected is not None:
                actual = await self._effort_label(menu)
                requested = selection.get("requested_reasoning_effort") or expected
                if (not self._effort_matches(expected, actual)
                        or not self._requested_effort_matches(requested, actual, allow_downgrade=selection.get("allow_downgrade") is True)):
                    raise BrowserTaskError(f"发送前档位验证失败：请求 {requested}，页面显示 {actual}；尚未发送。")
                selection["resolved_reasoning_effort"] = actual
            if selection["resolved_model"] is not None:
                _, actual_model = await self._read_model_options(menu)
                if actual_model != selection["resolved_model"]:
                    raise BrowserTaskError("发送前模型发生变化；尚未发送。")
            selection["selection_verified_at"] = utc_now()
        finally:
            await self._close_intelligence_menu(page)

    async def submit(
        self, mode: str, prompt: str, *, model: str | None = None,
        quality: str = "auto", reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        attachments: list[InMemoryUpload | FileRecord] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        on_submission_prepared: Callable[[SubmitReceipt], Awaitable[None]] | None = None,
        dedicated_page: bool = False,
    ) -> SubmitReceipt:
        page, receipt = await self._prepare_submit(
            mode, prompt, model=model, quality=quality, reasoning_effort=reasoning_effort,
            allow_downgrade=allow_downgrade, attachments=attachments,
            on_upload_verified=on_upload_verified, on_submission_prepared=on_submission_prepared,
            dedicated_page=dedicated_page,
        )
        return await self._finish_submission(page, receipt, mode=mode)

    async def _finish_submission(self, page: Page, receipt: SubmitReceipt, *, mode=None) -> SubmitReceipt:
        # Read-only acknowledgement must not keep the shared UI admission lock.
        conversation_url_confirmed = False
        try:
            await self._wait_for_conversation_url(page)
            conversation_url_confirmed = True
        except (BrowserTaskError, PlaywrightError, TimeoutError) as exc:
            # A click acknowledgement is not proof of delivery. Retain the
            # prepared identity and the exact page for read-only reconciliation.
            submission_phase("conversation_url_unconfirmed", error_type=type(exc).__name__, observed_url=page.url)
        receipt.conversation_url = page.url
        try:
            await self._confirm_submission(page, receipt)
        except (BrowserTaskError, PlaywrightError, TimeoutError) as exc:
            submission_phase("submission_confirmation_unavailable", error_type=type(exc).__name__)
        if not conversation_url_confirmed:
            receipt.submission_state = "unconfirmed"
        if receipt.submission_state == "confirmed":
            submission_phase("send_confirmed")
        # Plans can arrive long after acknowledgement. The polling lifecycle
        # persists a one-use intent before start_research(), never here.
        return receipt

    @_serialize_preparation
    async def _prepare_submit(
        self,
        mode: str,
        prompt: str,
        *,
        model: str | None = None,
        quality: str = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        attachments: list[InMemoryUpload | FileRecord] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        on_submission_prepared: Callable[[SubmitReceipt], Awaitable[None]] | None = None,
        dedicated_page: bool = False,
    ) -> tuple[Page, SubmitReceipt]:
        page = await (self._fresh_conversation_page() if dedicated_page else self._page())
        preparation = self._new_composer_preparation(page, prompt, mode)
        # Every new task gets a fresh ChatGPT conversation. Continuations use
        # continue_conversation() and deliberately keep the original URL.
        if page.url.rstrip("/") != "https://chatgpt.com" and not (
            dedicated_page and page in self._owned_pages
        ):
            await self._check_composer_draft(page, required=False)
            await page.goto(
                "https://chatgpt.com/",
                wait_until="domcontentloaded",
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        prepared_page = await self._prepare_submission_page(
            page, needs_selection=bool(model or reasoning_effort or quality != "auto"),
            can_reload=dedicated_page and page in self._owned_pages,
        )
        if prepared_page is not None:
            page = prepared_page
            preparation.page = page
        async with self._ui_transaction(page):
            try:
                composer = await self._check_composer_draft(page)
            except BrowserPageNotReady:
                # Focus/account hydration can replace a composer after the
                # first ready check. Recover once, still before any input.
                if not dedicated_page or not await self._can_recover_submission_page(page):
                    raise
                submission_phase("page_recovery_before_first_input")
                recovered = await self._prepare_submission_page(
                    page, needs_selection=bool(model or reasoning_effort or quality != "auto"), can_reload=True)
                page = recovered or page
                preparation.page = page
                await page.bring_to_front()
                composer = await self._check_composer_draft(page)
            attachment_receipt: list[dict[str, Any]] = []
            try:
                if mode == "research":
                    # Tool activation retains this body; filling after the app
                    # mention would erase Research. Claim before the first edit.
                    await self._fill_prepared_draft(page, preparation)
                    preparation.tool_transition = True
                    self._draft_preparation_phase(preparation, "research_tool_activating")
                    await self._activate_research(page)
                    await self._check_composer_draft(page, expected_text=prompt, preparation=preparation)
                    self._draft_preparation_phase(preparation, "research_tool_prepared",
                        rendered_text_sha256=preparation.rendered_text_sha256)
                    preparation.tool_transition = False
                prepared_text = prompt if mode == "research" else None
                await self._check_composer_draft(page, expected_text=prepared_text, preparation=preparation)
                selection = await self._apply_selection(
                    page, quality=quality, model=model, reasoning_effort=reasoning_effort,
                    allow_downgrade=allow_downgrade,
                )
                await self._check_composer_draft(page, expected_text=prepared_text, preparation=preparation)
                preparation.uploads_started = True
                attachment_receipt = await self._upload_files(page, attachments or [])
                if attachment_receipt and on_upload_verified:
                    await on_upload_verified(attachment_receipt)
                composer = await self._check_composer_draft(page, expected_text=prepared_text, preparation=preparation)
                baseline = await self._read_turn_surface(page)
                if baseline.get("verification_required"):
                    raise BrowserNeedsUserAction("verification_required", _verification_instructions(baseline))
                if baseline.get("runtime_error_code"):
                    raise BrowserPageNotReady("ChatGPT 站点在发送前出现运行时告警；尚未发送。")
                if baseline.get("unidentified_message_count"):
                    raise BrowserPageNotReady("ChatGPT 已有消息缺少可验证的回合身份，无法安全识别本轮回答；尚未发送。")
                before = sum(message["role"] == "assistant" for message in baseline["messages"])
                composer = await self._check_composer_draft(page, expected_text=prepared_text, preparation=preparation)
                if mode != "research":
                    await self._fill_prepared_draft(page, preparation)
                await self._check_composer_draft(page, expected_text=prompt, preparation=preparation)
                await self._verify_selection_after_upload(page, selection)
                await self._check_composer_draft(page, expected_text=prompt, preparation=preparation)
                self._draft_preparation_phase(preparation, "draft_prepared",
                    rendered_text_sha256=preparation.rendered_text_sha256, observed_page_url=page.url)
                receipt = SubmitReceipt(
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
                    user_message_ids_before=[message["id"] for message in baseline["messages"] if message["role"] == "user"],
                    assistant_message_ids_before=[message["id"] for message in baseline["messages"] if message["role"] == "assistant"],
                    submitted_prompt_sha256=_prompt_text_sha256(prompt),
                    submission_state="unconfirmed",
                    research_expected=(mode == "research"),
                )
                if on_submission_prepared:
                    await on_submission_prepared(receipt)
                self._scope.send_attempted = True
                self._draft_preparation_phase(preparation, "send_attempted")
                try:
                    await self._click_send(page)
                except (BrowserTaskError, PlaywrightError, TimeoutError) as exc:
                    submission_phase("send_dispatch_uncertain", error_type=type(exc).__name__)
            except BaseException as exc:
                # Unknown or human-edited drafts fail the ownership proof and
                # retain both body and attachments. Attempted sends never clean.
                await self._failed_composer_preparation(page, preparation, attachments or [], exc)
                raise
        return page, receipt

    async def continue_conversation(
        self, conversation_url: str, prompt: str, *, model: str | None = None,
        mode: str = "consult",
        quality: str = "auto", reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        attachments: list[InMemoryUpload | FileRecord] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        on_submission_prepared: Callable[[SubmitReceipt], Awaitable[None]] | None = None,
    ) -> SubmitReceipt:
        page, receipt = await self._prepare_continuation(
            conversation_url, prompt, mode=mode, model=model, quality=quality,
            reasoning_effort=reasoning_effort, allow_downgrade=allow_downgrade,
            attachments=attachments, on_upload_verified=on_upload_verified,
            on_submission_prepared=on_submission_prepared,
        )
        return await self._finish_submission(page, receipt, mode=mode)

    @_serialize_preparation
    async def _prepare_continuation(
        self,
        conversation_url: str,
        prompt: str,
        *,
        mode: str = "consult",
        model: str | None = None,
        quality: str = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        attachments: list[InMemoryUpload | FileRecord] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        on_submission_prepared: Callable[[SubmitReceipt], Awaitable[None]] | None = None,
    ) -> tuple[Page, SubmitReceipt]:
        page = await self._conversation_page(conversation_url)
        preparation = self._new_composer_preparation(page, prompt, mode)
        await self._require_composer(page, conversation_url=conversation_url)
        await self._require_conversation_route(page, conversation_url)
        async with self._ui_transaction(page):
            await self._check_composer_draft(page)
            attachment_receipt: list[dict[str, Any]] = []
            try:
                if mode == "research":
                    await self._fill_prepared_draft(page, preparation)
                    preparation.tool_transition = True
                    self._draft_preparation_phase(preparation, "research_tool_activating")
                    await self._activate_research(page)
                    await self._check_composer_draft(page, expected_text=prompt, preparation=preparation)
                    self._draft_preparation_phase(preparation, "research_tool_prepared",
                        rendered_text_sha256=preparation.rendered_text_sha256)
                    preparation.tool_transition = False
                prepared_text = prompt if mode == "research" else None
                await self._check_composer_draft(page, expected_text=prepared_text, preparation=preparation)
                selection = await self._apply_selection(
                    page, quality=quality, model=model, reasoning_effort=reasoning_effort,
                    allow_downgrade=allow_downgrade,
                )
                await self._check_composer_draft(page, expected_text=prepared_text, preparation=preparation)
                preparation.uploads_started = True
                attachment_receipt = await self._upload_files(page, attachments or [])
                if attachment_receipt and on_upload_verified:
                    await on_upload_verified(attachment_receipt)
                baseline = await self._read_turn_surface(page)
                if baseline.get("verification_required"):
                    raise BrowserNeedsUserAction("verification_required", _verification_instructions(baseline))
                if baseline.get("runtime_error_code"):
                    raise BrowserPageNotReady("ChatGPT 站点在发送前出现运行时告警；尚未发送。")
                if baseline.get("unidentified_message_count"):
                    raise BrowserPageNotReady("ChatGPT 已有消息缺少可验证的回合身份，无法安全识别本轮回答；尚未发送。")
                before = sum(message["role"] == "assistant" for message in baseline["messages"])
                composer = await self._check_composer_draft(page, expected_text=prepared_text, preparation=preparation)
                if mode != "research":
                    await self._fill_prepared_draft(page, preparation)
                await self._require_conversation_route(page, conversation_url)
                await self._check_composer_draft(page, expected_text=prompt, preparation=preparation)
                await self._verify_selection_after_upload(page, selection)
                await self._check_composer_draft(page, expected_text=prompt, preparation=preparation)
                self._draft_preparation_phase(preparation, "draft_prepared",
                    rendered_text_sha256=preparation.rendered_text_sha256, observed_page_url=page.url)
                receipt = SubmitReceipt(
                    page.url, before, selected_model=model,
                    requested_quality=quality, requested_reasoning_effort=reasoning_effort,
                    resolved_model=selection["resolved_model"],
                    resolved_reasoning_effort=selection["resolved_reasoning_effort"],
                    selection_source=selection["selection_source"],
                    selection_verified_at=selection["selection_verified_at"],
                    attachments=attachment_receipt,
                    user_message_ids_before=[message["id"] for message in baseline["messages"] if message["role"] == "user"],
                    assistant_message_ids_before=[message["id"] for message in baseline["messages"] if message["role"] == "assistant"],
                    submitted_prompt_sha256=_prompt_text_sha256(prompt),
                    submission_state="unconfirmed",
                    research_expected=(mode == "research"),
                )
                if on_submission_prepared:
                    await on_submission_prepared(receipt)
                self._scope.send_attempted = True
                self._draft_preparation_phase(preparation, "send_attempted")
                try:
                    await self._click_send(page)
                except (BrowserTaskError, PlaywrightError, TimeoutError) as exc:
                    submission_phase("send_dispatch_uncertain", error_type=type(exc).__name__)
            except BaseException as exc:
                await self._failed_composer_preparation(page, preparation, attachments or [], exc)
                raise
        return page, receipt

    async def _require_conversation_route(self, page: Page, conversation_url: str) -> None:
        actual, expected = urlparse(page.url), urlparse(conversation_url)
        if actual.scheme == "https" and actual.hostname in {"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"} and actual.path.rstrip("/") == expected.path.rstrip("/"):
            return
        await self._check_account_page_error(page)
        await self._check_verification_challenge(page)
        if await self._page_requires_login(page):
            raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。")
        raise BrowserPageNotReady("目标会话页面未保持在请求的会话地址；未发送续聊，不能将首页或其他会话视为原会话。")

    async def _wait_for_conversation_url(self, page: Page) -> None:
        def is_committed(url: Any) -> bool:
            parsed = urlparse(str(url))
            match = re.search(r"/c/([^/]+)/?$", parsed.path)
            return bool(
                parsed.hostname in {"chatgpt.com", "chat.openai.com"}
                and match and not unquote(match.group(1)).startswith("local-chatgpt:")
            )

        try:
            await page.wait_for_url(
                is_committed, wait_until="domcontentloaded",
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
        except PlaywrightError as exc:
            raise BrowserTaskError("已尝试发送，但尚未取得正式会话地址；发送状态未知，请检查当前标签页，勿重复提交。") from exc

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
        page, temporary_page = await self._auxiliary_conversation_page(conversation_url)
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
            if expected_name and not _matches_interpreter_filename(expected_name, file_name):
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
            if temporary_page:
                try:
                    await page.goto(
                        conversation_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.settings.browser_timeout_seconds * 1000),
                    )
                except PlaywrightError:
                    pass
            else:
                observed_request = await self._resident_conversation_request(page, conversation_id)
                if observed_request:
                    await _authenticated_page_json_get(
                        page, observed_request,
                        _client_api_headers({}, urlparse(observed_request).path),
                    )
            logged_in = False
            explicit_login_page = False
            ready_deadline = loop.time() + max(3.0, self.settings.browser_timeout_seconds)
            while loop.time() < ready_deadline:
                if _has_authenticated_conversation_response(page_events):
                    logged_in = True
                    break
                try:
                    explicit_login_page = await self._page_requires_login(page)
                except PlaywrightError:
                    explicit_login_page = False
                if explicit_login_page:
                    break
                try:
                    if await self._page_has_composer(page):
                        logged_in = True
                        break
                except PlaywrightError:
                    pass
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
            await self._close_auxiliary_page(page, temporary_page)
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
          const current = Array.from(document.querySelectorAll(
            '[data-content-search-unit-key$=":user"], [data-chatgpt-selection-message-id]:has([data-markdown-text-style="assistant-message"])'
          ));
          const nodes = (marked.length ? marked : current.length ? current : Array.from(document.querySelectorAll('article')))
            .filter(node => node.getClientRects().length);
          const messages = [];
          for (const node of nodes) {
            const role = node.getAttribute('data-message-author-role') ||
              (node.querySelector('[data-markdown-text-style="assistant-message"]') ? 'assistant' : null) ||
              ((node.getAttribute('data-content-search-unit-key') || '').endsWith(':user') ? 'user' : null) ||
              (node.matches('article') && /assistant|user/i.test(node.getAttribute('data-testid') || '')
                ? ((node.getAttribute('data-testid') || '').toLowerCase().includes('assistant') ? 'assistant' : 'user') : null);
            if (!role || !['user','assistant','system','tool'].includes(role)) continue;
            const text = (node.innerText || node.textContent || '').trim();
            const id = node.getAttribute('data-message-id') || node.getAttribute('data-chatgpt-selection-message-id') ||
              node.getAttribute('data-content-search-unit-key') || node.getAttribute('data-testid') || ('msg_' + (messages.length + 1));
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
                  const messageSelector = '[data-message-author-role], article, [data-content-search-unit-key$=":user"], [data-chatgpt-selection-message-id]';
                  const hasMessages = el => Boolean(el.querySelector(messageSelector));
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
                  const nodes = [...document.querySelectorAll(messageSelector)];
                  const messageIds = [...document.querySelectorAll('[data-message-id], [data-chatgpt-selection-message-id], [data-content-search-unit-key$=":user"]')]
                    .map(node => node.getAttribute('data-message-id') || node.getAttribute('data-chatgpt-selection-message-id') || node.getAttribute('data-content-search-unit-key'))
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
            async with self._ui_transaction(page):
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
                            async with self._ui_transaction(page):
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

        conversation_url = f"{source.scheme}://{source.netloc}/c/{quote(conversation_id, safe='')}"
        page, temporary_page = await self._auxiliary_conversation_page(conversation_url)
        try:
            request_headers = await self._conversation_request_headers(page, conversation_url, navigate=temporary_page)
            client_headers = _client_api_headers(request_headers, source.path)
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
            await self._close_auxiliary_page(page, temporary_page)

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
        conversation_url = f"{source.scheme}://{source.netloc}/c/{quote(conversation_id, safe='')}"
        request_headers = await self._conversation_request_headers(
            page, conversation_url, navigate=page.url.rstrip("/") != conversation_url.rstrip("/"),
        )
        client_headers = _client_api_headers(request_headers, source.path)

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
            target_url = f"{parsed.scheme}://{parsed.netloc}/c/{quote(conversation_id, safe='')}"
            page, temporary_page = await self._auxiliary_conversation_page(target_url)
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
                await self._close_auxiliary_page(page, temporary_page)
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
            upload_info = [item.metadata if isinstance(item, InMemoryUpload) else item for item in uploads]
            candidates: list[tuple[int, Any]] = []
            for index in range(await inputs.count()):
                candidate = inputs.nth(index)
                accept = (await candidate.get_attribute("accept") or "").strip().lower()
                types = [value.strip() for value in accept.split(",") if value.strip()]
                if types and "*/*" not in types:
                    compatible = all(any(
                        value == item.mime_type.split(";", 1)[0].strip().lower()
                        or (value.startswith(".") and item.filename.lower().endswith(value))
                        or (value.endswith("/*") and item.mime_type.lower().startswith(value[:-1]))
                        for value in types
                    ) for item in upload_info)
                    if not compatible:
                        continue
                if len(uploads) > 1 and await candidate.get_attribute("multiple") is None:
                    continue
                # Generic file handlers accept ZIP/documents; image/camera
                # handlers may silently ignore incompatible set_input_files.
                candidates.append((0 if not types or "*/*" in types else 1, candidate))
            if not candidates:
                raise BrowserTaskError("找不到能接收这些文件类型的 ChatGPT 上传控件。")
            last_error: Exception | None = None
            for _, candidate in sorted(candidates, key=lambda entry: entry[0]):
                try:
                    await candidate.set_input_files(payloads)
                    last_error = None
                    break
                except PlaywrightError as exc:
                    last_error = exc
            if last_error:
                raise BrowserTaskError("ChatGPT 文件上传控件无法接收文件。") from last_error
        names: list[str] = []
        for name in display_names:
            locator = self._attachment_remove_buttons(page, name)
            try:
                await locator.first.wait_for(state="visible", timeout=10000)
            except PlaywrightError:
                fallback = page.get_by_text(re.compile(re.escape(name), re.I)).filter(visible=True)
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
            locator = self._attachment_remove_buttons(page, name)
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible():
                        # Current cards hide the remove hit target until hover.
                        await candidate.hover(force=True, timeout=1000)
                        await candidate.click(timeout=3000)
                        break
                except PlaywrightError:
                    continue

    @staticmethod
    def _attachment_remove_buttons(page: Page, name: str) -> Locator:
        return page.get_by_role(
            "button",
            name=re.compile(rf"^(?:移除(?:文件)?|Remove(?: file)?)\s*{re.escape(name)}$", re.I),
        ).filter(visible=True)

    async def _cleanup_failed_uploads(self, page: Page, uploads: list[InMemoryUpload | FileRecord]) -> None:
        try:
            await self._clear_uploaded_files(page, self._unique_display_names(uploads))
        except Exception:
            # Cleanup is best effort; retain the actual upload/selection/send
            # failure rather than replacing it with a secondary exception.
            logging.getLogger(__name__).exception("Attachment cleanup failed; preserving the original submission error")

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
        if not await plus.count():
            plus = page.get_by_role(
                "button", name=re.compile(r"^(添加文件等(?:内容)?|Add files and more)$", re.I)
            )
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

    async def _read_turn_surface(self, page: Page) -> dict[str, Any]:
        """Read stable, visible turn identities in one bounded page operation."""
        script = r"""
        () => {
          const visible = node => Boolean(node && node.getClientRects().length);
          const selector = '[data-message-author-role], [data-content-search-unit-key$=":user"], ' +
            '[data-content-search-unit-key$=":assistant"], ' +
            '[data-chatgpt-search-unit-key$=":user"], [data-chatgpt-search-unit-key$=":assistant"], ' +
            '[data-chatgpt-selection-message-id]:has([data-markdown-text-style="assistant-message"]), ' +
            'article[data-testid*="conversation-turn-assistant"], article[data-testid*="conversation-turn-user"]';
          const identity = node => {
            const owner = node.closest('[data-message-id], [data-chatgpt-selection-message-id], [data-content-search-unit-key], [data-chatgpt-search-unit-key]');
            // New UI search wrappers contain the canonical message node.
            // Treat that nested representation as one message, while leaving
            // wrappers with multiple genuine messages/fragments separate.
            if (node.hasAttribute('data-content-search-unit-key') || node.hasAttribute('data-chatgpt-search-unit-key')) {
              const canonicalIds = [...new Set([...node.querySelectorAll('[data-message-id], [data-chatgpt-selection-message-id]')]
                .map(child => child.getAttribute('data-message-id') || child.getAttribute('data-chatgpt-selection-message-id')).filter(Boolean))];
              if (canonicalIds.length === 1) return canonicalIds[0];
            }
            return node.getAttribute('data-message-id') || node.getAttribute('data-chatgpt-selection-message-id') ||
              owner?.getAttribute('data-message-id') || owner?.getAttribute('data-chatgpt-selection-message-id') ||
              (node.getAttribute('data-content-search-unit-key') || node.getAttribute('data-chatgpt-search-unit-key') ||
                owner?.getAttribute('data-content-search-unit-key') || owner?.getAttribute('data-chatgpt-search-unit-key') || '')
                .replace(/:(user|assistant)$/, '');
          };
          const roleOf = node => node.getAttribute('data-message-author-role') ||
            ((node.getAttribute('data-chatgpt-search-unit-key') || '').endsWith(':user') ? 'user' : null) ||
            ((node.getAttribute('data-chatgpt-search-unit-key') || '').endsWith(':assistant') ? 'assistant' : null) ||
            ((node.getAttribute('data-content-search-unit-key') || '').endsWith(':user') ? 'user' : null) ||
            ((node.getAttribute('data-content-search-unit-key') || '').endsWith(':assistant') ? 'assistant' : null) ||
            (node.querySelector('[data-markdown-text-style="assistant-message"]') ? 'assistant' : null) ||
            ((node.getAttribute('data-testid') || '').includes('conversation-turn-assistant') ? 'assistant' : null) ||
            ((node.getAttribute('data-testid') || '').includes('conversation-turn-user') ? 'user' : null);
          const rawUserText = node => {
            // Markdown rendering removes syntax and adds collapse controls.
            // Read only the item attached to this rendered message, bound to
            // its DOM message IDs; never scan global application state or
            // weaken the exact submitted-text hash comparison.
            try {
              const owner = node.closest('[data-chatgpt-search-message-ids]');
              const ids = (owner?.getAttribute('data-chatgpt-search-message-ids') || '')
                .match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi) || [];
              if (!ids.length) return null;
              const key = Object.getOwnPropertyNames(node).find(name => name.startsWith('__reactFiber$'));
              let fiber = key ? node[key] : null;
              for (let depth = 0; fiber && depth < 8; depth++, fiber = fiber.return) {
                const item = fiber.memoizedProps?.item;
                if (item?.type !== 'user-message' || typeof item.message !== 'string') continue;
                return [item.serverMessageId, item.messageId].some(id => typeof id === 'string' && ids.includes(id))
                  ? item.message : null;
              }
            } catch (_) { /* Missing renderer evidence stays unconfirmed. */ }
            return null;
          };
          const activePattern = /^(?:Pro\s*)?(?:Searching|Researching|Finalizing|Thinking|思考中|正在思考|正在搜索|正在研究|正在生成)(?:\b|\s|[.…:：\d]|$)/i;
          const proseText = node => {
            if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
            if (node.nodeType !== Node.ELEMENT_NODE || !visible(node) || node.matches('blockquote, pre, code')) return '';
            if (node.tagName === 'BR') return '\n';
            return [...node.childNodes].map(proseText).join('') + (/^(?:P|DIV|LI|H[1-6])$/.test(node.tagName) ? '\n' : '');
          };
          const messages = [], seen = new Set();
          let unidentifiedMessages = 0;
          for (const node of document.querySelectorAll(selector)) {
            if (!visible(node)) continue;
            if (node.hasAttribute('data-content-search-unit-key') || node.hasAttribute('data-chatgpt-search-unit-key')) {
              const nestedIds = [...new Set([...node.querySelectorAll('[data-message-id], [data-chatgpt-selection-message-id]')]
                .map(child => child.getAttribute('data-message-id') || child.getAttribute('data-chatgpt-selection-message-id')).filter(Boolean))];
              if (nestedIds.length > 1) continue;
            }
            const role = roleOf(node);
            if (!['user', 'assistant'].includes(role)) continue;
            const authorNode = node.querySelector('[data-message-author-role="' + role + '"]') || node;
            const id = identity(authorNode);
            if (!id) {
              if (!node.querySelector('[data-message-id], [data-chatgpt-selection-message-id], [data-content-search-unit-key]')) unidentifiedMessages++;
              continue;
            }
            if (seen.has(role + ':' + id)) continue;
            seen.add(role + ':' + id);
            const markdowns = role === 'assistant' ? [
              ...(authorNode.matches('[data-markdown-text-style="assistant-message"]') ? [authorNode] : []),
              ...authorNode.querySelectorAll('[data-markdown-text-style="assistant-message"]')] : [];
            const finalMarkdown = markdowns.find(element => element.getAttribute('data-markdown-text-tone') !== 'tertiary');
            // The new activity view renders thinking/tool summaries as
            // tertiary assistant markdown; they are not a final answer.
            const researchWidget = [...node.querySelectorAll('iframe')].some(element => visible(element) &&
              (/^(?:深度研究|Deep research)$/i.test(element.getAttribute('title') || '') ||
               /(?:internal:\/\/deep-research|deep-research)/i.test(element.getAttribute('src') || '')));
            const researchPlanControls = [...node.querySelectorAll('button, [role="button"]')].filter(element => visible(element) &&
              !element.disabled && element.getAttribute('aria-disabled') !== 'true' &&
              /^(?:Start research|开始研究|启动研究|开始深度研究)$/i.test((element.getAttribute('aria-label') || element.innerText || '').trim()));
            const researchPlanReady = researchPlanControls.length > 0;
            if (role === 'assistant' && markdowns.length && !finalMarkdown && !researchWidget && !researchPlanReady) continue;
            const content = role === 'assistant' ? finalMarkdown || authorNode : authorNode;
            const text = (content.innerText || '').trim();
            // File tiles and turn controls may surround the actual user text.
            // Retain bounded candidate containers instead of deleting content
            // or accepting a substring match of the submitted prompt.
            const promptCandidates = role === 'user'
              ? [...node.querySelectorAll('div, p, pre')].filter(visible).slice(0, 100)
                .map(element => (element.innerText || '').trim()).filter(Boolean) : [];
            const sources = [...node.querySelectorAll('a[href]')].map(link => link.href).filter(href => {
              try { const url = new URL(href); return ['http:', 'https:'].includes(url.protocol) &&
                !['chatgpt.com', 'chat.openai.com'].includes(url.hostname); } catch { return false; }
            });
            const active = node.getAttribute('aria-busy') === 'true' || [...node.querySelectorAll('button, [role="button"]')].some(element =>
              visible(element) && /^(?:Stop generating|停止生成)$/i.test((element.getAttribute('aria-label') || element.innerText || '').trim())) ||
              [...node.querySelectorAll('[role="status"], [role="progressbar"]')]
              .some(element => visible(element) && (activePattern.test((element.getAttribute('aria-label') || element.innerText || '').trim()) ||
                (element.getAttribute('role') === 'progressbar' &&
                  (element.getAttribute('aria-valuenow') === null || Number(element.getAttribute('aria-valuenow')) < Number(element.getAttribute('aria-valuemax') || 100)))));
            const containerIds = [];
            for (let owner = authorNode; owner; owner = owner.parentElement) {
              const ownerId = identity(owner);
              if (ownerId && !containerIds.includes(ownerId)) containerIds.push(ownerId);
              const searchKey = (owner.getAttribute('data-content-search-unit-key') || owner.getAttribute('data-chatgpt-search-unit-key') || '').replace(/:(user|assistant)$/, '');
              if (searchKey && !containerIds.includes(searchKey)) containerIds.push(searchKey);
            }
            const originalUserText = role === 'user' ? rawUserText(node) : null;
            if (originalUserText !== null) {
              // Native Research serializes a routing chip as Markdown after
              // the user's text. Remove only that exact suffix when this same
              // rendered user message proves a single native routing chip.
              const nativeChips = [...node.querySelectorAll(
                '[data-prompt-link-href="app://connector_openai_deep_research"][data-prompt-link-label="$app"]')]
                .filter(element => visible(element) && !element.closest('pre, code, blockquote'));
              const routing = '[$app](app://connector_openai_deep_research)';
              if (nativeChips.length === 1 && originalUserText.endsWith(routing)) {
                promptCandidates.push(originalUserText.slice(0, -routing.length));
              }
            }
            const researchRuns = [...new Set([node, ...node.querySelectorAll('[data-research-run-id], [data-research-id], [data-tool-call-id]')]
              .flatMap(element => ['data-research-run-id', 'data-research-id', 'data-tool-call-id'].map(name => element.getAttribute(name))).filter(Boolean))];
            const controlledIds = element => (element.getAttribute('aria-controls') || '').split(/\s+/).filter(Boolean).concat(
              element.getAttribute('href')?.startsWith('#') ? [element.getAttribute('href').slice(1)] : []);
            const ownsPanel = (element, panelId) => {
              const refs = [...document.querySelectorAll('[aria-controls], a[href^="#"]')].filter(other => visible(other) && controlledIds(other).includes(panelId));
              const panel = document.getElementById(panelId);
              const activeRefs = refs.filter(other => other.getAttribute('aria-expanded') === 'true' || other.getAttribute('aria-selected') === 'true' ||
                (other.id && (panel?.getAttribute('aria-labelledby') || '').split(/\s+/).includes(other.id)));
              return activeRefs.length === 1 && activeRefs[0] === element;
            };
            const researchPanels = [...new Set([...node.querySelectorAll('[aria-controls], a[href^="#"]')]
              .flatMap(element => controlledIds(element).filter(panelId => ownsPanel(element, panelId))))];
            const intentText = proseText(content).trim();
            const researchHint = researchWidget || researchPlanReady || researchRuns.length > 0 ||
              (intentText.length < 500 && /^(?:(?:I(?: have|['’]ve)?|We(?: have|['’]ve)?) (?:started|begun|initiated) (?:the )?(?:deep )?research|(?:The )?(?:Deep )?research (?:(?:has |is )?(?:started|underway|in progress))|(?:已开始|正在进行|已启动)(?:深度)?研究|(?:Deep\s+Research|深度研究|研究)\s*(?:已开始|已启动|进行中))(?:[.!。！,，:：\s]|$)/i.test(intentText));
            messages.push({id, role, text, ...(originalUserText !== null ? {prompt_text: originalUserText} : {}),
              clarification_candidate_text: role === 'assistant' ? proseText(content).trim() : null,
              container_ids: containerIds, prompt_text_candidates: [...new Set(promptCandidates)], sources,
              active, has_research_widget: researchWidget, research_hint: researchHint,
              research_plan_ready: researchPlanReady, research_run_ids: researchRuns, research_panel_ids: researchPanels});
          }
          const stopPattern = /^(?:Stop generating|停止生成|Stop|停止|停止研究|Stop research)$/i;
          const researchPanel = '[data-research-panel], [data-testid="deep-research-panel"], [data-testid="research-panel"], ' +
            '[data-research-report], [role="dialog"][aria-label*="research" i], [role="complementary"][aria-label*="research" i], ' +
            '[role="dialog"][aria-label*="研究"], [role="complementary"][aria-label*="研究"]';
          const turnStates = [...document.querySelectorAll('[data-talvt-turn-state]')].filter(visible);
          const turnState = turnStates.at(-1)?.getAttribute('data-talvt-turn-state') || null;
          const provisionalConversation = [...document.querySelectorAll('[data-map-composer-conversation]')].some(node =>
            node.getAttribute('data-map-composer-conversation')?.startsWith('local-chatgpt:'));
          const generationActive = turnState === 'in_progress' || [...document.querySelectorAll('button, [role="button"]')].some(node =>
            visible(node) && !node.closest(selector) && !node.closest(researchPanel) &&
              (node.getAttribute('data-testid') === 'stop-button' ||
              stopPattern.test((node.getAttribute('aria-label') || node.innerText || '').trim()))) ||
            [...document.querySelectorAll('[role="status"], [role="progressbar"]')].some(node =>
              visible(node) && /^ChatGPT 正在回应$/i.test((node.getAttribute('aria-label') || '').trim()));
          const dialogNodes = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog[open]')].filter(node =>
            visible(node) && !node.matches(researchPanel) && !node.querySelector(
              '[data-research-report], iframe[title="Deep research" i], iframe[title="深度研究"]'));
          const dialogs = dialogNodes.map(node => (node.innerText || '').trim()).join('\n');
          const outsideTurn = node => !node.closest(selector) && !node.querySelector(selector);
          const loginRequired = [...document.querySelectorAll('input[type="email"], input[name="username"]')].some(node => visible(node) && outsideTurn(node)) ||
            [...document.querySelectorAll('button, a, [role="button"], [role="link"]')].some(node => visible(node) && outsideTurn(node) &&
              /^(?:登录|登入|免费注册|Log in|Sign in|Sign up(?: for free)?)$/i.test((node.getAttribute('aria-label') || node.innerText || '').trim()));
          const verification = (__CHATGPT_VERIFICATION_PROBE__)();
          const challenge = verification.verification_required;
          return {messages, unidentified_message_count: unidentifiedMessages, generation_active: generationActive,
            generation_state:turnState, provisional_conversation:provisionalConversation, page_title: document.title,
            login_required: loginRequired, verification_required: challenge, site_alert_code: verification.site_alert_code,
            runtime_error_code: verification.runtime_error_code,
            blocking_dialog: dialogNodes.length > 0,
            blocking_text: challenge ? (verification.site_alert_code || 'verify you are human') + '\n' + dialogs : dialogs,
            empty_page_text: messages.length ? '' : (document.body?.innerText || '')};
        }
        """.replace("__CHATGPT_VERIFICATION_PROBE__", _CHATGPT_VERIFICATION_PROBE)
        try:
            async with asyncio.timeout(min(5.0, self.settings.browser_timeout_seconds)):
                value = await page.evaluate(script)
        except TimeoutError as exc:
            self.mark_unresponsive()
            raise BrowserUnresponsiveError("ChatGPT current-turn observation exceeded its deadline; the request will not be resent.") from exc
        except PlaywrightError as exc:
            raise BrowserTaskError("ChatGPT current-turn observation failed; sending must not be retried automatically.") from exc
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            raise BrowserTaskError("ChatGPT current-turn observation returned an invalid payload.")
        return value

    async def _confirm_submission(self, page: Page, receipt: SubmitReceipt) -> None:
        """Observe acknowledgement of one click, never click again on ambiguity."""
        budget = min(10.0, self.settings.browser_timeout_seconds)
        deadline = asyncio.get_running_loop().time() + budget
        try:
            async with asyncio.timeout(budget):
                while asyncio.get_running_loop().time() < deadline:
                    surface = await self._read_turn_surface(page)
                    _resolve_current_turn(receipt, surface["messages"])
                    # Capture the initial assistant shell as well, when it
                    # appears within the same short budget. It lets subsequent
                    # polls survive eviction of a long user prompt.
                    if receipt.submission_state == "confirmed" and receipt.current_assistant_message_id:
                        return
                    await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
        except (TimeoutError, BrowserTaskError):
            # The click may already have sent the question. Preserve an
            # explicitly unconfirmed receipt for the caller to report; neither
            # recovery nor the client may repeat the prompt on this evidence.
            if receipt.submission_state != "confirmed":
                receipt.submission_state = "unconfirmed"

    async def request_stop(self, receipt: SubmitReceipt) -> bool:
        """Stop only the matched workflow, then require observed termination."""
        try:
            page = await self._conversation_page(receipt.conversation_url, create=False)
        except BrowserTaskError:
            return False
        stop_pattern = re.compile(r"^(Stop generating|停止生成|Stop|停止|停止研究|Stop research)$", re.I)
        clicked = False
        deadline = asyncio.get_running_loop().time() + min(8.0, self.settings.browser_timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self.inspect(receipt)
            if receipt.submission_state != "confirmed" or not receipt.current_user_message_id:
                return False
            if snapshot.user_input_kind == "manual_browser":
                return False
            if snapshot.generation_active is False:
                if not clicked and snapshot.state == "RUNNING" and snapshot.research_state != "stopped":
                    return False  # A quiet pending acknowledgement is not a stopped workflow.
                if snapshot.research_state and snapshot.research_state not in {"complete", "stopped", "plan_ready"}:
                    return False
                if snapshot.research_state == "plan_ready" and receipt.research_start_state:
                    return False
                return bool(await self._page_has_composer(page) and not await self._page_requires_login(page))
            if not clicked:
                surface = await self._read_turn_surface(page)
                users = [message for message in surface["messages"] if message.get("role") == "user"]
                if not users or users[-1]["id"] != receipt.current_user_message_id:
                    return False
                latest = users[-1]
                if not any(
                    isinstance(text, str) and _prompt_text_sha256(text) == receipt.submitted_prompt_sha256
                    for text in [latest.get("prompt_text", latest.get("text", "")), *latest.get("prompt_text_candidates", [])]
                ):
                    return False
                async with self._ui_transaction(page):
                    output = _resolve_current_turn(receipt, surface["messages"])
                    ids = list(dict.fromkeys(identity for identity in [receipt.current_user_message_id,
                        receipt.current_assistant_message_id, receipt.research_assistant_message_id,
                        *(identity for message in output for identity in [message["id"], *message.get("container_ids", [])])] if identity))
                    run_ids = ([receipt.research_run_id] if receipt.research_run_id else
                               list(dict.fromkeys(identity for message in output for identity in message.get("research_run_ids", []))))
                    panel_ids = list(dict.fromkeys(identity for message in output for identity in message.get("research_panel_ids", [])))
                    research_workflow = bool(receipt.research_observed or snapshot.research_state not in {None, "report_pending"})
                    if research_workflow:
                        targets, _ = await self._research_targets(page, current_message_ids=ids,
                            current_run_ids=run_ids, current_panel_ids=panel_ids)
                        stop_targets = [(target, is_frame) for target, is_frame, _metadata in targets]
                    else:
                        # The composer stop button is safe only after proving
                        # this is still the latest exact-hash user turn. Never
                        # scan unrelated frames for a convenient Stop button.
                        stop_targets = [(getattr(page, "main_frame", page), True)]
                    for target, is_frame in stop_targets:
                        controls = target.get_by_role("button", name=stop_pattern)
                        for index in range(await controls.count()):
                            control = controls.nth(index)
                            if await control.is_visible() and await control.is_enabled():
                                await control.click(timeout=2000)
                                clicked = True
                                break
                        if research_workflow and not clicked:
                            nested = await self._nested_research_control(target, is_frame, stop_pattern)
                            if nested.get("count") == 1:
                                clicked = True  # At most one potentially consumed stop action.
                                action = await self._nested_research_control(target, is_frame, stop_pattern, click=True)
                                if not action.get("clicked"):
                                    return False
                        if clicked:
                            break
            await page.wait_for_timeout(100)
        return False

    async def inspect_submission(self, receipt: SubmitReceipt) -> BrowserSnapshot:
        """Keep observing the original target; never navigate or submit again."""
        page = self._scope.primary_page
        if page is None or page.is_closed():
            page = await self._session_page()
        if page is None or page.is_closed():
            raise BrowserTaskError("Original pending ChatGPT page is unavailable; no question was resent")
        surface = await self._read_turn_surface(page)
        _resolve_current_turn(receipt, surface['messages'])
        parsed = urlparse(page.url)
        match = re.search(r'/c/([^/]+)/?$', parsed.path)
        committed = (parsed.hostname in {'chatgpt.com', 'chat.openai.com'} and match
                     and not unquote(match.group(1)).startswith('local-chatgpt:'))
        login_required = _is_explicit_login_page(page.url) or surface.get("login_required")
        if surface.get("verification_required") or login_required:
            if not committed:
                receipt.submission_state = "unconfirmed"
            return BrowserSnapshot(
                "NEEDS_USER_INPUT", page.url if committed else None,
                user_action="请在专用浏览器 Profile 中登录 ChatGPT。" if login_required else _verification_instructions(surface),
                user_input_kind="manual_browser",
                page_title=surface.get("page_title"), current_user_message_id=receipt.current_user_message_id,
                current_assistant_message_id=receipt.current_assistant_message_id,
                submission_state=receipt.submission_state, submission_verified_at=receipt.submission_verified_at,
                generation_active=bool(surface.get("generation_active")),
            )
        if surface.get("runtime_error_code"):
            if not committed:
                receipt.submission_state = "unconfirmed"
            return self._runtime_error_snapshot(receipt, surface, page.url if committed else None)
        if committed and receipt.submission_state == 'confirmed':
            receipt.conversation_url = page.url
            submission_phase('send_confirmed_late')
            return await self.inspect(receipt)
        receipt.submission_state = 'unconfirmed'
        return BrowserSnapshot('SEND_PENDING', None, progress='ChatGPT 正在建立原会话；保留同一任务等待，不重发。',
                               submission_state='unconfirmed')

    async def inspect(self, receipt: SubmitReceipt) -> BrowserSnapshot:
        page = await self._conversation_page(receipt.conversation_url)
        surface = await self._read_turn_surface(page)
        if surface.get("runtime_error_code"):
            target = self._owned_pages.get(page)
            # Deduplicate the same visible failure across 30 s polls.
            last = probe_snapshot(target)[-1:]
            if not (last and last[0].get("kind") == "surface_runtime_error"
                    and last[0].get("code") == surface.get("runtime_error_code")
                    and last[0].get("page_url") == str(page.url).replace("https://chatgpt.com", "")):
                probe_event(target, "surface_runtime_error",
                            code=surface.get("runtime_error_code"), page_url=page.url,
                            generation_active=surface.get("generation_active"),
                            generation_state=surface.get("generation_state"),
                            provisional_conversation=surface.get("provisional_conversation"),
                            observed_messages=len(surface.get("messages") or []))
        if (receipt.submission_state == "confirmed" and receipt.current_user_message_id
                and receipt.current_user_message_id.startswith("fallback-turn-")
                and self._runtime_route_matches(page.url, receipt.conversation_url)
                and not surface.get("verification_required") and not surface.get("login_required")):
            # A normal site retry can commit the provisional user ID without
            # reloading. Bind only the unique latest original exact-hash turn;
            # ambiguity keeps the previous identity and cannot yield a result.
            try:
                self._bind_runtime_recovery_turn(receipt, surface)
            except BrowserRuntimeRecoveryError:
                pass
        output = _resolve_current_turn(receipt, surface["messages"])
        # The site can drop a committed turn back to its home screen. That is
        # an observed failure of this turn, never evidence that it is still
        # thinking; polling a home page until the 1800 s budget is the hang.
        routed_home = self._runtime_route_is_home(page.url) and self._runtime_route_matches(
            receipt.conversation_url, receipt.conversation_url)
        if (routed_home and not output and not surface.get("generation_active")
                and not surface.get("verification_required") and not surface.get("login_required")):
            probe_event(self._owned_pages.get(page), "conversation_route_left", page_url=page.url,
                        expected=receipt.conversation_url, observed_messages=len(surface["messages"]))
            return self._runtime_error_snapshot(
                receipt, {**surface, "runtime_error_code": "conversation_route_left"}, None)
        if ((receipt.runtime_recovery_attempts or self._scope.runtime_recovery_attempts)
                and receipt.current_user_message_id and receipt.current_user_message_id.startswith("fallback-turn-")
                and not receipt.current_assistant_message_id and not surface.get("generation_active")
                and not surface.get("verification_required") and not surface.get("login_required")
                and not any(message["id"] == receipt.current_user_message_id for message in surface["messages"])
                and self._runtime_route_matches(page.url, receipt.conversation_url)):
            missing = {**surface, "runtime_error_code": "runtime_turn_missing"}
            return self._runtime_error_snapshot(receipt, missing, page.url)
        if (surface.get("runtime_error_code") and not output and not surface.get("generation_active")
                and not surface.get("verification_required")
                and not surface.get("login_required") and not _is_explicit_login_page(page.url)):
            return self._runtime_error_snapshot(receipt, surface, page.url)
        current_ids = list(dict.fromkeys(identity for identity in [
            receipt.current_user_message_id, receipt.current_assistant_message_id,
            receipt.research_assistant_message_id,
            *(identity for message in output for identity in [message["id"], *message.get("container_ids", [])]),
        ] if identity))
        current_runs = ([receipt.research_run_id] if receipt.research_run_id else
                        list(dict.fromkeys(identity for message in output for identity in message.get("research_run_ids", []))))
        current_panels = list(dict.fromkeys(identity for message in output for identity in message.get("research_panel_ids", [])))
        research_surface = await self._research_frame_surface(
            page, current_message_ids=current_ids, current_run_ids=current_runs, current_panel_ids=current_panels,
        )
        research_text = research_surface["text"]
        result = "\n\n".join(str(message.get("text") or "").strip() for message in output if message.get("text")) or None
        research_hint = any(message.get("research_hint") for message in output)
        research_rendered = any(message.get("has_research_widget") or message.get("research_plan_ready")
                                or message.get("research_run_ids") for message in output)
        if research_surface["matched"] or research_rendered:
            receipt.research_observed = True
            receipt.research_user_message_id = receipt.current_user_message_id
            receipt.research_user_prompt_sha256 = receipt.submitted_prompt_sha256
            receipt.research_assistant_message_id = receipt.current_assistant_message_id
        elif research_hint:
            # An acknowledgement predicts a late app; it does not prove that
            # a research run is already visible or has actually started.
            receipt.research_expected = True
        research_context = bool(receipt.research_expected or receipt.research_observed or research_surface["unassociated"])
        plan_ready = bool(research_surface["plan_ready"] or any(message.get("research_plan_ready") for message in output))
        report_complete = bool(research_surface["complete"])
        if research_context:
            result = research_text if report_complete else None
            receipt.research_state = research_surface["state"] or ("plan_ready" if plan_ready else "report_pending")
            receipt.research_run_id = research_surface.get("run_id") or receipt.research_run_id
            receipt.research_report_id = research_surface.get("report_id") or receipt.research_report_id
        else:
            receipt.research_state = None  # A vanished ownerless portal was never attributed to this turn.
        current_text = "\n".join(str(message.get("text") or "") for message in output)
        active = bool(surface.get("generation_active")) or any(message.get("active") for message in output) or research_surface["active"]
        # Browser blockers are observed independently of assistant prose. An
        # answer discussing login or CAPTCHA is not itself an account screen.
        blocking_text = str(surface.get("blocking_text") or "")
        environment_text = "\n".join(value for value in (
            page.url if _is_explicit_login_page(page.url) else "",
            blocking_text,
            str(surface.get("empty_page_text") or "") if not surface["messages"] else "",
        ) if value)
        environment_state, environment_action = classify_page_text(environment_text, has_new_output=False)
        manual_browser = bool(
            _is_explicit_login_page(page.url) or surface.get("login_required") or surface.get("verification_required")
            or surface.get("blocking_dialog") or blocking_text or environment_state == "NEEDS_USER_INPUT"
        )
        clarification_candidate = str(output[-1].get("text") or "") if output else ""
        clarification_prose = str(output[-1].get("clarification_candidate_text", output[-1].get("text")) or "") if output else ""
        state, user_action = classify_page_text(
            clarification_prose, has_new_output=bool(output), has_research_output=bool(research_text), active_hint=active,
        )
        content_clarification = bool(state == "NEEDS_USER_INPUT" and not active and not report_complete and not
                                     research_surface["active"] and not research_surface["unassociated"] and not plan_ready)
        if research_context and not content_clarification:
            # Missing activity labels are not a terminal event. Only a matched
            # completed report may finish a research turn, in every request mode.
            if plan_ready and not active and receipt.research_start_state is None:
                state = "RESEARCH_PLAN_READY"
            elif report_complete and not active:
                state = "COMPLETE"
            else:
                state = "RUNNING"
                # Local pending observation is not upstream activity evidence.
                # Keep generation_active tied to actual page controls/status.
        clarification_text = None
        user_input_kind = None
        if manual_browser:
            state = "NEEDS_USER_INPUT"
            user_input_kind = "manual_browser"
            user_action = environment_action or (
                "请在专用浏览器 Profile 中登录 ChatGPT。" if _is_explicit_login_page(page.url) or surface.get("login_required") else
                _verification_instructions(surface) if surface.get("verification_required") else
                "ChatGPT 页面有阻塞对话框，请在专用浏览器中处理后继续观察本轮任务。"
            )
        elif (
            state == "NEEDS_USER_INPUT" and not active and receipt.submission_state == "confirmed"
            and receipt.current_user_message_id and receipt.current_assistant_message_id and output
            and output[-1]["id"] == receipt.current_assistant_message_id
            and clarification_candidate.strip() and not is_transient_assistant_text(clarification_candidate)
        ):
            clarification_text = clarification_candidate.strip()
            user_input_kind = "content_clarification"
        if not receipt.current_user_message_id:
            if state != "NEEDS_USER_INPUT":
                state = "NEEDS_USER_INPUT"
                user_action = "无法确认当前问题已发送：缺少新用户回合的稳定身份。请检查该会话，勿重复提交相同问题。"
        elif not result or is_transient_assistant_text(result):
            if state not in {"NEEDS_USER_INPUT", "RESEARCH_PLAN_READY"}:
                state = "RUNNING"
            result = None
        if state == "NEEDS_USER_INPUT" and user_input_kind is None:
            user_input_kind = "manual_browser"
        progress = None
        for line in "\n".join(value for value in (current_text, research_text) if value).splitlines():
            if any(token in line.lower() for token in ("search", "research", "搜索", "研究", "引用", "source")):
                progress = line.strip()[:400]
        if progress is None and state == "RUNNING":
            if not output and surface.get("generation_state") == "in_progress":
                progress = "网页仍标记当前回合进行中，尚未出现可关联的正式助手回答；继续观察原问题。"
            else:
                progress = "当前问题已在页面确认，正在等待本轮回答。" if not output else "正在读取当前回合的回答。"
        last_checked_at = utc_now()
        # A provisional (local-chatgpt:...) conversation has not been committed
        # by the server. If it is still the only turn after a full poll cycle,
        # upstream never accepted the send; time out early instead of watching
        # a static page for the whole budget.
        if (not receipt.current_assistant_message_id and receipt.submission_state == "confirmed"
                and surface.get("provisional_conversation") and len(surface["messages"]) <= 1
                and not receipt.provisional_uncommitted_since):
            receipt.provisional_uncommitted_since = utc_now()
        elif (receipt.current_assistant_message_id or not surface.get("provisional_conversation")
              or len(surface["messages"]) > 1):
            receipt.provisional_uncommitted_since = None
        if receipt.provisional_uncommitted_since:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(receipt.provisional_uncommitted_since)).total_seconds()
            if elapsed >= min(180.0, self.settings.browser_timeout_seconds * 6):
                probe_event(self._owned_pages.get(page), "provisional_uncommitted", page_url=page.url,
                            elapsed_seconds=round(elapsed, 1), generation_active=surface.get("generation_active"),
                            generation_state=surface.get("generation_state"))
                return BrowserSnapshot(
                    "RECOVERABLE_ERROR", None,
                    progress="发送后会话始终未被服务器确认；服务停止继续等待并尝试恢复原回合。",
                    runtime_error_code="provisional_uncommitted",
                    page_title=surface.get("page_title"),
                    current_user_message_id=receipt.current_user_message_id,
                    submission_state=receipt.submission_state,
                    submission_verified_at=receipt.submission_verified_at,
                    generation_active=bool(surface.get("generation_active")),
                )
        if state == "RUNNING" and not output:
            submission_phase("chatgpt_turn_pending", generation_active=active,
                generation_state=surface.get("generation_state"),
                provisional_conversation=surface.get("provisional_conversation"),
                user_message_id=receipt.current_user_message_id,
                assistant_message_id=receipt.current_assistant_message_id,
                observed_messages=len(surface["messages"]))
        if research_context:
            progress = research_surface.get("progress") or (
                "发现无法关联到当前回合的研究面板，等待确认归属；尚未读取其中报告。" if research_surface["unassociated"] else
                "研究计划已就绪，等待一次性启动。" if state == "RESEARCH_PLAN_READY" else
                "研究启动已请求，等待原计划的启动确认。" if plan_ready and receipt.research_start_state else
                "研究已停止，尚未取得完成报告。" if research_surface["stopped"] else
                "研究报告尚未具备完成证据，继续观察原回合。" if state == "RUNNING" else progress
            )
        sources = (research_surface["sources"] if research_context else
                   list(dict.fromkeys(href for message in output for href in message.get("sources", []))))[:30]
        return BrowserSnapshot(
            state=state, conversation_url=page.url, progress=progress, user_action=user_action,
            result_text=result if state == "COMPLETE" else None, sources=sources,
            page_title=surface.get("page_title"), current_user_message_id=receipt.current_user_message_id,
            current_assistant_message_id=receipt.current_assistant_message_id,
            submission_state=receipt.submission_state, submission_verified_at=receipt.submission_verified_at,
            generation_active=active,
            clarification_text=clarification_text, user_input_kind=user_input_kind,
            research_observed=receipt.research_observed, research_state=receipt.research_state,
            research_plan_ready=state == "RESEARCH_PLAN_READY", research_report_complete=report_complete,
            research_run_id=receipt.research_run_id, research_report_id=receipt.research_report_id,
        )

    @staticmethod
    def _runtime_error_snapshot(receipt: SubmitReceipt, surface: dict[str, Any], conversation_url: str | None) -> BrowserSnapshot:
        return BrowserSnapshot(
            "RECOVERABLE_ERROR", conversation_url,
            progress="ChatGPT 页面出现运行时告警，服务正在恢复原回合。",
            runtime_error_code=surface.get("runtime_error_code"),
            page_title=surface.get("page_title"), current_user_message_id=receipt.current_user_message_id,
            current_assistant_message_id=receipt.current_assistant_message_id,
            submission_state=receipt.submission_state, submission_verified_at=receipt.submission_verified_at,
            generation_active=bool(surface.get("generation_active")),
        )

    @staticmethod
    def _runtime_route_is_home(actual: str) -> bool:
        current = urlparse(actual)
        return bool(current.scheme == "https" and current.hostname in {"chatgpt.com", "chat.openai.com"}
                    and not current.path.rstrip("/"))

    @staticmethod
    def _runtime_route_matches(actual: str, expected: str) -> bool:
        current, original = urlparse(actual), urlparse(expected)
        match = re.fullmatch(r"/c/([^/]+)/?", original.path)
        return bool(
            original.scheme == current.scheme == "https" and original.hostname == current.hostname
            and original.hostname in {"chatgpt.com", "chat.openai.com"} and match
            and not unquote(match.group(1)).startswith("local-chatgpt:")
            and current.path.rstrip("/") == original.path.rstrip("/")
        )

    @staticmethod
    def _bind_runtime_recovery_turn(receipt: SubmitReceipt, surface: dict[str, Any]) -> list[dict[str, Any]]:
        """Rebind only a unique latest exact-hash user on the same known route."""
        if receipt.submission_state != "confirmed" or not receipt.current_user_message_id or not receipt.submitted_prompt_sha256:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_UNCONFIRMED", "运行时恢复缺少已确认的原用户回合；未重新提交问题。")
        users = [message for message in surface["messages"] if message.get("role") == "user"]
        matches = [message for message in users if any(
            isinstance(text, str) and _prompt_text_sha256(text) == receipt.submitted_prompt_sha256
            for text in [message.get("prompt_text", message.get("text", "")), *message.get("prompt_text_candidates", [])]
        )]
        if not users or not matches:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_TURN_NOT_VISIBLE", "原回合的完整文本尚不可见，无法核对发送 hash；未重试生成。")
        if len(matches) != 1:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_TURN_AMBIGUOUS", "多个用户回合具有同一发送 hash，无法唯一关联；未重试生成。")
        current = matches[0]
        if current is not users[-1]:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_TURN_MISMATCH", "页面最新用户回合已变化，未操作其他回合。")
        if current["id"] != receipt.current_user_message_id:
            if (receipt.current_user_message_id.startswith("fallback-turn-")
                    and not current["id"].startswith("fallback-turn-")
                    and current["id"] not in receipt.user_message_ids_before):
                # Reload can replace the original provisional DOM identity.
                # The original formal route plus a unique, latest exact hash
                # establishes the binding; never invent or substring-match IDs.
                receipt.current_user_message_id = current["id"]
            else:
                raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_TURN_MISMATCH", "原用户身份与当前页面不一致；未重试生成。")
        return _resolve_current_turn(receipt, surface["messages"])

    async def _runtime_recovery_surface(self, page: Page, receipt: SubmitReceipt, *, allow_missing: bool = False) -> tuple[dict[str, Any], bool]:
        surface = await self._read_turn_surface(page)
        if _is_explicit_login_page(page.url) or surface.get("login_required"):
            raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。")
        if surface.get("verification_required"):
            raise BrowserNeedsUserAction("verification_required", _verification_instructions(surface))
        if (self._runtime_route_is_home(page.url)
                and self._runtime_route_matches(receipt.conversation_url, receipt.conversation_url)):
            # The tab lost the conversation after the send. Reopening the
            # original route is a read-only recovery: it never sends or
            # creates a turn, and the caller requires a later unique exact-hash
            # binding before any answer is accepted.
            return {**surface, "runtime_error_code": "conversation_route_left"}, True
        if not self._runtime_route_matches(page.url, receipt.conversation_url):
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_ROUTE_MISMATCH", "运行时恢复只允许原正式会话地址，当前页面已变化。")
        missing = False
        try:
            output = self._bind_runtime_recovery_turn(receipt, surface)
        except BrowserRuntimeRecoveryError as exc:
            last = surface["messages"][-1] if surface["messages"] else {}
            if (allow_missing and exc.code == "RUNTIME_RECOVERY_TURN_NOT_VISIBLE"
                    and receipt.current_user_message_id and receipt.current_user_message_id.startswith("fallback-turn-")
                    and receipt.assistant_message_ids_before
                    and last.get("role") == "assistant" and last.get("id") == receipt.assistant_message_ids_before[-1]):
                # The site's Retry can discard its optimistic user message.
                # Only a read-only reload is allowed here, never another send.
                output, missing = [], True
            elif (exc.code == "RUNTIME_RECOVERY_TURN_NOT_VISIBLE"
                    and surface.get("runtime_error_code") == "site_conversation_load_failed"
                    and receipt.assistant_message_ids_before):
                # The load-failure card replaces the conversation; nothing is
                # readable to bind or accept until a recovery re-renders it.
                output, missing = [], True
            elif (exc.code == "RUNTIME_RECOVERY_TURN_NOT_VISIBLE"
                    and surface.get("runtime_error_code") == "conversation_route_left"
                    and receipt.current_user_message_id
                    and receipt.current_user_message_id.startswith("fallback-turn-")):
                output, missing = [], True
            else:
                raise
        if (surface.get("generation_active") or any(message.get("active") for message in output)
                or output or receipt.current_assistant_message_id):
            return surface, False
        if surface.get("runtime_error_code") not in {"cloudflare_challenge", "conversation_route_left", "site_conversation_load_failed"} and not missing:
            return surface, False
        try:
            await self._check_composer_draft(page, required=False)
        except BrowserComposerDraftPresent as exc:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_DRAFT_PRESENT", "原页面出现人工草稿，服务保留草稿并停止运行时恢复。") from exc
        state = await page.evaluate(r"""() => {
            const visible = node => Boolean(node.getClientRects().length);
            const draft = [...document.querySelectorAll('textarea, [contenteditable="true"]')].some(node =>
                String(node.value ?? node.textContent ?? '').trim() || node.querySelector('img,video,audio,canvas,svg,[contenteditable="false"]'));
            const files = [...document.querySelectorAll('input[type="file"]')].some(node => node.files?.length);
            const attachments = [...document.querySelectorAll('button,[role="button"]')].some(node => visible(node) &&
                /Remove(?: file)?|移除(?:文件)?/i.test(node.getAttribute('aria-label') || node.getAttribute('title') || ''));
            const upload = [...document.querySelectorAll('[data-testid*="attachment"],[data-testid*="upload"],[data-upload-id],[data-composer-body] [role="progressbar"]')].some(visible);
            return {draft: Boolean(draft), attachments: files || attachments || upload};
        }""")
        if not isinstance(state, dict):
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_COMPOSER_UNKNOWN", "无法验证原页面的草稿和附件状态；未恢复生成。")
        if state.get("draft"):
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_DRAFT_PRESENT", "原页面有草稿，服务保留原内容并停止运行时恢复。")
        if state.get("attachments"):
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_ATTACHMENTS_PRESENT", "原页面有未发送附件，服务保留附件并停止运行时恢复。")
        return surface, True

    async def _runtime_retry_control(self, page: Page) -> Locator | None:
        matches: list[Locator] = []
        alerts = page.get_by_role("alert").filter(visible=True)
        for index in range(await alerts.count()):
            alert = alerts.nth(index)
            valid = await alert.evaluate(r"""alert => {
                const turns = '[data-message-author-role],[data-message-id],[data-content-search-unit-key],[data-chatgpt-selection-message-id],article[data-testid*="conversation-turn"]';
                const excluded = turns + ',blockquote,pre,code,[hidden],[aria-hidden="true"],[inert]';
                if (alert.closest(excluded) || alert.querySelector(turns)) return false;
                for (let owner = alert; owner; owner = owner.parentElement) {
                    const style = getComputedStyle(owner);
                    if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
                }
                return [alert, ...alert.querySelectorAll('div,p,span')].some(node => node.getClientRects().length &&
                    !node.closest(excluded) && getComputedStyle(node).visibility === 'visible' &&
                    (node.innerText || '').trim() === 'cloudflare_challenge');
            }""")
            if not valid:
                continue
            controls = alert.get_by_role("button", name=re.compile(r"^(?:重试|再试一次|重新尝试|Retry|Try again)$", re.I)).filter(visible=True)
            for button_index in range(await controls.count()):
                control = controls.nth(button_index)
                if await control.is_enabled():
                    matches.append(control)
        return matches[0] if len(matches) == 1 else None

    async def recover_runtime_error(
        self, receipt: SubmitReceipt, *, attempt: int,
        on_recovery_prepared: Callable[[str], Awaitable[None]],
    ) -> bool:
        """Consume one prepared recovery action for the original failed turn.

        False means no action was prepared. True includes a prepared action
        safely skipped because output arrived during the durable checkpoint.
        Neither path submits a new user turn.
        """
        if type(attempt) is not int or attempt not in {1, 2}:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_LIMIT", "运行时恢复最多允许两次，服务没有执行额外操作。")
        if attempt in self._scope.runtime_recovery_attempts:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_ALREADY_CONSUMED", "本次运行时恢复动作已消费，不重复执行。")
        if receipt.submission_state != "confirmed" or not self._runtime_route_matches(receipt.conversation_url, receipt.conversation_url):
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_UNCONFIRMED", "只有已确认、具有原正式地址的回合允许运行时恢复。")
        try:
            page = self._scope.primary_page
            if page is None or page.is_closed():
                page = await self._conversation_page(receipt.conversation_url, create=False)
        except BrowserTaskError as exc:
            raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_PAGE_UNAVAILABLE", "原会话页面不可用；服务没有创建新问题或新标签页。") from exc
        async with self._resources.ui_locked():
            async with asyncio.timeout(self.settings.browser_timeout_seconds):
                _, safe = await self._runtime_recovery_surface(page, receipt, allow_missing=attempt == 2)
                if not safe:
                    return False
                routed_home = self._runtime_route_is_home(page.url)
                control = await self._runtime_retry_control(page) if attempt == 1 else None
                if control is None and attempt == 1:
                    # The load-failure card owns its own retry button too.
                    controls = page.get_by_role("button", name=re.compile(
                        r"^(?:再试一次|重试|Try again|Retry)$", re.I)).filter(visible=True)
                    control = controls.first if await controls.count() == 1 else None
                action = ("open_conversation_route" if routed_home else
                          "retry_site_alert" if control is not None else "reload_conversation")
                # The owner persists consumption and checks cancellation here.
                # Failure/cancellation of this callback performs no UI action.
                await on_recovery_prepared(action)
                self._scope.runtime_recovery_attempts.add(attempt)
                _, safe = await self._runtime_recovery_surface(page, receipt, allow_missing=attempt == 2)
                if not safe:
                    return True
                if action == "open_conversation_route":
                    submission_phase("runtime_recovery_reopen", attempt=attempt)
                    try:
                        await page.goto(receipt.conversation_url, wait_until="domcontentloaded",
                                        timeout=max(1, int(self.settings.browser_timeout_seconds * 1000)))
                    except PlaywrightError as exc:
                        raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_RELOAD_FAILED", "原会话重新打开未完成；该动作已消费，没有重新提交问题。") from exc
                    # The turn identity is re-established by the next
                    # observation from the formal route. Never send here.
                    return True
                if action == "retry_site_alert":
                    control = await self._runtime_retry_control(page)
                    if control is None:
                        return True
                    submission_phase("runtime_recovery_retry", attempt=attempt)
                    try:
                        await control.click(timeout=min(2000, max(1, int(self.settings.browser_timeout_seconds * 1000))))
                    except PlaywrightError as exc:
                        # Acknowledgement can fail after dispatch. Consume this
                        # one click and observe the original turn. Any second
                        # recovery is a read-only reload, never another click.
                        submission_phase("runtime_recovery_retry_unconfirmed", attempt=attempt, error_type=type(exc).__name__)
                    return True
                submission_phase("runtime_recovery_reload", attempt=attempt)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=max(1, int(self.settings.browser_timeout_seconds * 1000)))
                except PlaywrightError as exc:
                    raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_RELOAD_FAILED", "原会话刷新未完成；该刷新动作已消费，没有重新提交问题。") from exc
                deadline = asyncio.get_running_loop().time() + min(5.0, self.settings.browser_timeout_seconds)
                while True:
                    refreshed = await self._read_turn_surface(page)
                    if _is_explicit_login_page(page.url) or refreshed.get("login_required"):
                        raise BrowserNeedsUserAction("login_required", "请在专用浏览器 Profile 中登录 ChatGPT。")
                    if refreshed.get("verification_required"):
                        raise BrowserNeedsUserAction("verification_required", _verification_instructions(refreshed))
                    if not self._runtime_route_matches(page.url, receipt.conversation_url):
                        raise BrowserRuntimeRecoveryError("RUNTIME_RECOVERY_ROUTE_MISMATCH", "刷新后已离开原正式会话地址；没有创建新问题。")
                    try:
                        self._bind_runtime_recovery_turn(receipt, refreshed)
                        return True
                    except BrowserRuntimeRecoveryError as exc:
                        if exc.code != "RUNTIME_RECOVERY_TURN_NOT_VISIBLE" or asyncio.get_running_loop().time() >= deadline:
                            raise
                    await asyncio.sleep(.1)

    async def _page_text(self, page: Page) -> str:
        for selector in ("main:visible", "body"):
            try:
                text = await page.locator(selector).inner_text(timeout=5000)
                if text.strip():
                    return text
            except PlaywrightError:
                continue
        return ""

    async def _research_frame_text(self, page: Page, *, current_message_ids: list[str] | None = None) -> str:
        return (await self._research_frame_surface(page, current_message_ids=current_message_ids))["text"]

    async def _research_targets(
        self, page: Page, *, current_message_ids: list[str] | None = None,
        current_run_ids: list[str] | None = None, current_panel_ids: list[str] | None = None,
    ) -> tuple[list[tuple[Any, bool, dict[str, Any]]], bool]:
        """Resolve DOM ownership before reading any research panel contents.

        A portal must have an explicit message/run ID or be the target of the
        current message's aria-controls/fragment link. A visible ownerless
        widget blocks completion, but its report must never be absorbed.
        """
        message_ids, run_ids, panel_ids = set(current_message_ids or []), set(current_run_ids or []), set(current_panel_ids or [])
        targets: list[tuple[Any, bool, dict[str, Any]]] = []
        frame_associations: dict[Any, tuple[str, dict[str, Any]]] = {}
        unassociated = False
        for frame in page.frames:
            if frame == getattr(page, "main_frame", None):
                continue
            parent = frame_associations.get(frame.parent_frame)
            try:
                element = await frame.frame_element()
                metadata = await element.evaluate(RESEARCH_ELEMENT_METADATA)
                if not isinstance(metadata, dict) or not metadata.get("visible"):
                    continue
                is_research = bool(parent) or "internal://deep-research" in frame.url or bool(
                    re.fullmatch(r"深度研究|Deep research", str(metadata.get("title") or ""), re.I)
                ) or "deep-research" in str(metadata.get("src") or "")
                if not is_research:
                    continue
                association = research_association(metadata, message_ids=message_ids, run_ids=run_ids, panel_ids=panel_ids)
                if parent:
                    # A child can inherit its known research frame's owner;
                    # explicit conflicting ownership still wins.
                    association = parent[0] if association == "unknown" else association
                frame_associations[frame] = (association, metadata)
                if association == "matched":
                    targets.append((frame, True, metadata))
                elif association == "unknown":
                    unassociated = True
            except PlaywrightError:
                # A detached/loading widget is not evidence of completion.
                if parent and parent[0] == "matched":
                    targets.append((frame, True, parent[1]))
                continue
        if hasattr(page, "locator"):
            panels = page.locator(RESEARCH_PANEL_SELECTOR)
            for index in range(await panels.count()):
                panel = panels.nth(index)
                try:
                    metadata = await panel.evaluate(RESEARCH_ELEMENT_METADATA)
                    if not isinstance(metadata, dict) or not metadata.get("visible"):
                        continue
                    if metadata.get("contains_research_frame"):
                        continue  # Its independently scoped frame is the report surface.
                    association = research_association(metadata, message_ids=message_ids, run_ids=run_ids, panel_ids=panel_ids)
                    if association == "matched":
                        targets.append((panel, False, metadata))
                    elif association == "unknown":
                        unassociated = True
                except PlaywrightError:
                    continue
        return targets, unassociated

    async def _research_frame_surface(
        self, page: Page, *, current_message_ids: list[str] | None = None,
        current_run_ids: list[str] | None = None, current_panel_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        values: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(min(5.0, self.settings.browser_timeout_seconds)):
                targets, unassociated = await self._research_targets(
                    page, current_message_ids=current_message_ids,
                    current_run_ids=current_run_ids, current_panel_ids=current_panel_ids,
                )
                for target, is_frame, metadata in targets:
                    try:
                        root = target.locator("body") if is_frame else target
                        value = await root.evaluate(NESTED_RESEARCH_CONTENT_PROBE, {"allow_generic_start": is_frame},
                                                    timeout=min(3000, int(self.settings.browser_timeout_seconds * 1000)))
                        nested = value if isinstance(value, list) else [value]
                        for content in nested:
                            if not isinstance(content, dict):
                                content = {}
                            if not content.get("run_id") and len(metadata.get("run_ids") or []) == 1:
                                content["run_id"] = metadata["run_ids"][0]
                            values.append(content)
                    except PlaywrightError:
                        values.append({})  # A matched empty/loading frame stays pending.
                return merge_research_surfaces(values, unassociated=unassociated)
        except TimeoutError as exc:
            self.mark_unresponsive()
            raise BrowserUnresponsiveError("ChatGPT current research observation exceeded its deadline.") from exc

    async def _nested_research_control(self, target, is_frame: bool, pattern: re.Pattern, *, click=False) -> dict:
        root = target.locator("body") if is_frame else target
        value = await root.evaluate(NESTED_RESEARCH_CONTROL,
                                    {"pattern": pattern.pattern, "action": "click" if click else "inspect"},
                                    timeout=min(2000, int(self.settings.browser_timeout_seconds * 1000)))
        return value if isinstance(value, dict) else {"count": 0, "clicked": False}

    async def _latest_result(self, page: Page) -> str | None:
        locator = await self._assistant_messages(page)
        count = await locator.count()
        if not count:
            return None
        try:
            text = await locator.nth(count - 1).inner_text(timeout=3000)
            return text.strip() or None
        except PlaywrightError:
            return None

    async def _assistant_messages(self, page: Page) -> Locator:
        for selector in (
            '[data-message-author-role="assistant"]:visible',
            "article[data-testid*='conversation-turn-assistant']:visible",
            '[data-chatgpt-selection-message-id]:has([data-markdown-text-style="assistant-message"]):visible',
        ):
            locator = page.locator(selector)
            if await locator.count():
                return locator
        # An empty assistant locator must stay empty. User turns and generic
        # articles do not prove that an assistant response has arrived.
        return page.locator('[data-message-author-role="assistant"]:visible')

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
        turns = page.locator('[data-talvt-turn-state]:visible')
        if await turns.count() and await turns.last.get_attribute('data-talvt-turn-state') == 'in_progress':
            return True
        # The current app widget can load after the assistant has already
        # acknowledged that research started. An empty widget is not a report.
        widgets = page.locator('iframe[title="深度研究"], iframe[title="Deep research"]')
        for index in range(await widgets.count()):
            if await widgets.nth(index).is_visible() and not await self._research_frame_text(page):
                return True
        patterns = (
            re.compile(r"^(Stop generating|停止生成|Stop|停止|停止研究|Stop research)$", re.I),
            re.compile(r"^ChatGPT 正在回应$", re.I),
            re.compile(r"^(?:Pro\s*)?(Searching|Researching|Finalizing|Thinking|思考中|正在思考|正在搜索|正在研究|正在生成)[.…]*$", re.I),
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
        return await (await self._assistant_messages(page)).count()

    async def _find_composer(self, page: Page, *, require_enabled: bool = True) -> Locator:
        selectors = (
            "textarea[placeholder*='message' i]:not(#pending-home-input)",
            "textarea[placeholder*='消息' i]:not(#pending-home-input)",
            "textarea:not(#pending-home-input)",
            "[contenteditable='true']",
        )
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible() and (not require_enabled or await candidate.is_enabled()):
                        return candidate
                except PlaywrightError:
                    continue
        role_locator = page.get_by_role("textbox")
        for index in range(await role_locator.count() - 1, -1, -1):
            candidate = role_locator.nth(index)
            if await candidate.get_attribute("id") == "pending-home-input":
                continue
            if await candidate.is_visible() and (not require_enabled or await candidate.is_enabled()):
                return candidate
        raise BrowserPageNotReady("找不到 ChatGPT 消息输入框；请检查专用浏览器页面或 UI 是否变化。")

    def _new_composer_preparation(self, page: Page, prompt: str, mode: str) -> ComposerPreparation:
        if self._scope.send_attempted or submission_send_attempted():
            raise BrowserPreparationError("本任务已记录发送尝试；只允许观察原回合，不能再次准备或发送。")
        preparation = ComposerPreparation(page, prompt, mode)
        self._draft_preparation_phase(preparation, "preparation_started",
            requested_mode=mode, observed_page_url=page.url,
            page_target_id=self._owned_pages.get(page), session_id=self._scope.session_id,
            request_text_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            submitted_prompt_sha256=_prompt_text_sha256(prompt))
        return preparation

    def _draft_preparation_phase(self, preparation: ComposerPreparation, phase: str, **details) -> None:
        if not submission_phase(phase, preparation_id=preparation.preparation_id, **details):
            raise BrowserPreparationError("无法持久化本任务的草稿准备证据；尚未发送。")

    async def _read_composer_draft(self, composer: Locator, **options) -> dict:
        value = await composer.evaluate(COMPOSER_DRAFT_SCRIPT, options,
            timeout=int(self.settings.browser_timeout_seconds * 1000))
        if not isinstance(value, dict):
            raise BrowserPreparationError("无法可靠读取 ChatGPT 草稿状态；尚未覆盖输入框。")
        return value

    async def _fill_prepared_draft(self, page: Page, preparation: ComposerPreparation) -> None:
        composer = await self._check_composer_draft(page)
        value = await self._read_composer_draft(composer, action="claim", preparation_id=preparation.preparation_id)
        if value.get("preparation_id") != preparation.preparation_id or value.get("interrupted"):
            raise BrowserComposerDraftPresent()
        self._draft_preparation_phase(preparation, "draft_claimed", observed_page_url=page.url,
            page_target_id=self._owned_pages.get(page), tool_present=False, send_attempted=False)
        # Ownership check and write share one browser task, so a draft arriving
        # after the checkpoint cannot be overwritten by a later fill().
        value = await self._read_composer_draft(composer, action="fill",
            preparation_id=preparation.preparation_id, signature=value.get("signature"), text=preparation.prompt)
        if value.get("preparation_id") != preparation.preparation_id or value.get("interrupted"):
            raise BrowserComposerDraftPresent()
        if value.get("changed") is not True:
            if (str(value.get("text") or "").strip() or value.get("has_mention")
                    or value.get("native_model_empty") is False):
                raise BrowserComposerDraftPresent()
            raise BrowserPreparationError("原生编辑器未确认接收本任务正文；尚未发送。")
        # A positively acknowledged atomic write is owned even when the
        # editor rejected/rewrote its content. It may be cleaned once, and is
        # a service preparation error rather than a human draft conflict.
        preparation.filled = True
        preparation.signature = value.get("signature")
        if _prompt_text_sha256(str(value.get("prompt_text") or "")) != _prompt_text_sha256(preparation.prompt):
            raise BrowserPreparationError("ChatGPT 原生编辑器没有保留本任务的准确正文；尚未发送。")
        preparation.rendered_text_sha256 = hashlib.sha256(str(value.get("prompt_text") or "").encode("utf-8")).hexdigest()
        self._draft_preparation_phase(preparation, "draft_filled",
            rendered_text_sha256=preparation.rendered_text_sha256, tool_present=bool(value.get("has_mention")))

    async def _cleanup_prepared_draft(self, page: Page, preparation: ComposerPreparation) -> bool:
        """One atomic cleanup, only for this attempt's positively owned editor."""
        if (self._scope.send_attempted or submission_send_attempted() or page is not preparation.page
                or not preparation.filled):
            return False
        try:
            composer = await self._find_composer(page, require_enabled=False)
            value = await self._read_composer_draft(composer)
            owned = (value.get("preparation_id") == preparation.preparation_id and not value.get("interrupted")
                and not value.get("foreign_mentions") and not value.get("has_embedded_content"))
            exact_body = _prompt_text_sha256(str(value.get("prompt_text") or "")) == _prompt_text_sha256(preparation.prompt)
            tool_body = (preparation.tool_transition and value.get("research_mentions") == 1
                and any(_prompt_text_sha256(str(candidate.get("prompt_text") or "")) == _prompt_text_sha256(preparation.prompt)
                        for candidate in value.get("separator_candidates", [])))
            known_signature = value.get("signature") == preparation.signature
            if not owned or not (known_signature or (preparation.tool_transition and (exact_body or tool_body))):
                submission_phase("draft_cleanup_preserved", preparation_id=preparation.preparation_id,
                    reason="ownership_or_content_changed")
                return False
            result = await self._read_composer_draft(composer, action="clear",
                preparation_id=preparation.preparation_id, signature=value.get("signature"))
            cleaned = result.get("cleared") is True
            if cleaned and result.get("native_model_present"):
                # DOM deletion can precede the native editor/application
                # update. Verify the model as well, and allow its persistence
                # effects to settle before the tab may be released.
                settle_step = min(0.2, self.settings.browser_timeout_seconds * 0.05)
                for _ in range(3):
                    await asyncio.sleep(settle_step)
                    settled = await self._read_composer_draft(composer)
                    if (str(settled.get("text") or "").strip() or settled.get("has_mention")
                            or settled.get("has_embedded_content") or settled.get("native_model_empty") is False):
                        cleaned = False
                        break
            submission_phase("draft_cleanup_finished", preparation_id=preparation.preparation_id,
                cleared=cleaned, native_model_empty=result.get("native_model_empty"), send_attempted=False)
            return cleaned
        except (BrowserTaskError, PlaywrightError, TimeoutError):
            submission_phase("draft_cleanup_preserved", preparation_id=preparation.preparation_id, reason="cleanup_unavailable")
            return False

    async def _failed_composer_preparation(self, page: Page, preparation: ComposerPreparation,
                                           attachments: list, error: BaseException) -> None:
        submission_phase("preparation_failed", preparation_id=preparation.preparation_id,
            error_type=type(error).__name__, send_attempted=bool(self._scope.send_attempted or submission_send_attempted()))
        if self._scope.send_attempted or submission_send_attempted():
            return
        cleaned = await self._cleanup_prepared_draft(page, preparation)
        if not preparation.filled:
            try:
                await self._check_composer_draft(page)
                cleaned = True
            except (BrowserTaskError, BrowserNeedsUserAction, PlaywrightError, TimeoutError):
                pass
        if cleaned and preparation.uploads_started:
            await self._cleanup_failed_uploads(page, attachments)

    async def _check_composer_draft(
        self, page: Page, *, expected_text: str | None = None, required: bool = True,
        preparation: ComposerPreparation | None = None,
    ) -> Locator | None:
        """Protect all unclaimed drafts; recognize only exact prepared content."""
        try:
            # A visible but temporarily disabled composer can still hold a
            # human draft; navigating or reloading must not erase it.
            composer = await self._find_composer(page, require_enabled=False)
        except BrowserTaskError:
            if required:
                raise
            return None
        value = await self._read_composer_draft(composer)
        if expected_text is None:
            if (str(value.get("text") or "").strip() or value.get("has_mention") or value.get("has_embedded_content")
                    or value.get("native_model_empty") is False):
                submission_phase("composer_draft_conflict", observed_page_url=page.url,
                    observed_text_sha256=hashlib.sha256(str(value.get("prompt_text") or "").encode("utf-8")).hexdigest(),
                    tool_present=bool(value.get("has_mention")))
                raise BrowserComposerDraftPresent()
        else:
            if preparation is not None and (
                    page is not preparation.page or value.get("preparation_id") != preparation.preparation_id
                    or value.get("interrupted") or value.get("foreign_mentions") or value.get("has_embedded_content")):
                submission_phase("draft_validation_failed", preparation_id=preparation.preparation_id,
                    reason="ownership_or_human_interaction", interrupted=bool(value.get("interrupted")),
                    observed_text_sha256=hashlib.sha256(str(value.get("prompt_text") or "").encode("utf-8")).hexdigest())
                raise BrowserComposerDraftPresent()
            expected_hash = _prompt_text_sha256(expected_text)
            if _prompt_text_sha256(str(value.get("prompt_text") or "")) != expected_hash:
                candidates = value.get("separator_candidates", []) if preparation and preparation.tool_transition else []
                matching = [candidate for candidate in candidates
                    if _prompt_text_sha256(str(candidate.get("prompt_text") or "")) == expected_hash]
                if len(matching) == 1:
                    candidate = matching[0]
                    value = await self._read_composer_draft(composer, action="repair_separator",
                        preparation_id=preparation.preparation_id, signature=value.get("signature"),
                        path=candidate["path"], edge=candidate["edge"])
                    if _prompt_text_sha256(str(value.get("prompt_text") or "")) == expected_hash:
                        self._draft_preparation_phase(preparation, "research_separator_repaired", removed_codepoint="U+0020")
                if _prompt_text_sha256(str(value.get("prompt_text") or "")) != expected_hash:
                    submission_phase("draft_validation_failed",
                        **({"preparation_id": preparation.preparation_id} if preparation else {}),
                        reason="tool_transform_mismatch" if preparation and preparation.tool_transition else "text_mismatch",
                        expected_prompt_sha256=expected_hash,
                        observed_text_sha256=hashlib.sha256(str(value.get("prompt_text") or "").encode("utf-8")).hexdigest(),
                        research_mentions=value.get("research_mentions", 0))
                    if preparation and preparation.tool_transition:
                        raise BrowserPreparationError("研究工具转换未保持本任务正文；未发送，已保留无法证明归属的内容。")
                    raise BrowserComposerDraftPresent()
            if preparation:
                preparation.signature = value.get("signature")
                preparation.rendered_text_sha256 = hashlib.sha256(str(value.get("prompt_text") or "").encode("utf-8")).hexdigest()
        return composer

    async def _click_send(self, page: Page) -> None:
        locator = page.locator("button[data-testid='send-button']")
        for index in range(await locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                await candidate.click(timeout=int(self.settings.browser_timeout_seconds * 1000))
                return
        locator = page.get_by_role(
            "button",
            name=re.compile(r"^(发送|发送消息|发送提示词|Send|Send message|Send prompt|提交|Submit)$", re.I),
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

    async def _require_research_tool(self, page: Page) -> None:
        deadline = asyncio.get_running_loop().time() + min(2, self.settings.browser_timeout_seconds)
        pattern = re.compile(r"^(深度研究|Deep research)$", re.I)
        while True:
            composer = await self._find_composer(page)
            state = await self._read_composer_draft(composer)
            if state.get("research_mentions") == 1 and not state.get("foreign_mentions"):
                return
            toolbar = composer.locator('xpath=ancestor::*[@data-composer-body or self::form][1]')
            controls = toolbar.get_by_role("button", name=pattern)
            for index in range(await controls.count()):
                candidate = controls.nth(index)
                if await candidate.is_visible() and await candidate.get_attribute("aria-pressed") == "true":
                    return
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserPreparationError("未观察到 Deep Research 工具已激活；尚未发送。")
            await asyncio.sleep(0.05)

    async def _activate_research(self, page: Page) -> None:
        # fill() does not promise the saved rich-editor selection. The app menu
        # inserts at that selection, which previously split Chinese words.
        composer = await self._find_composer(page)
        if await composer.locator('[app-mention-path="app://connector_openai_deep_research"]').count() == 1:
            return
        pattern = re.compile(r"^(深度研究|Deep research)$", re.I)
        toolbar = composer.locator('xpath=ancestor::*[@data-composer-body or self::form][1]')
        controls = toolbar.get_by_role("button", name=pattern)
        for index in range(await controls.count()):
            candidate = controls.nth(index)
            if await candidate.is_visible() and await candidate.get_attribute("aria-pressed") == "true":
                return
        await composer.evaluate(r"""node => {
            node.focus();
            if ('selectionStart' in node) {
                node.setSelectionRange(node.value.length, node.value.length);
            } else {
                const selection = window.getSelection(), range = document.createRange();
                range.selectNodeContents(node); range.collapse(false);
                selection.removeAllRanges(); selection.addRange(range);
            }
        }""")
        # Historical user messages also render a clickable Research mention.
        # Only a freshly opened composer menu may activate this tool.
        plus = page.locator("button[data-testid='composer-plus-btn']")
        if not await plus.count():
            plus = page.get_by_role("button", name=re.compile(r"^(添加文件等(?:内容)?|Add files and more)$", re.I))
        try:
            await plus.first.wait_for(state="visible", timeout=5000)
        except PlaywrightError as exc:
            raise BrowserTaskError("当前 ChatGPT 页面没有找到 Deep Research 工具菜单。") from exc
        for index in range(await plus.count()):
            candidate = plus.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                await candidate.click()
                action_pattern = re.compile(r"^(深度研究|Deep research)(?:\s|$)", re.I)
                menu = page.locator('[data-mention-section-id]:visible, [role="menu"]:visible')
                action = (menu.locator('button[data-plugin-impression-id="connector_openai_deep_research"]')
                          .or_(menu.get_by_role("button", name=action_pattern))
                          .or_(menu.get_by_role("menuitem", name=action_pattern)))
                try:
                    await action.first.wait_for(state="visible", timeout=5000)
                except PlaywrightError as exc:
                    raise BrowserTaskError("当前 ChatGPT 页面没有找到 Deep Research 入口。") from exc
                await action.first.click()
                await self._require_research_tool(page)
                return
        # The menu may be behind the composer '+' / attachment control.
        for name in (r"添加文件等", r"Add", r"附件", r"Attach"):
            locator = toolbar.get_by_role("button", name=re.compile(name, re.I))
            for index in range(await locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click()
                    await page.wait_for_timeout(250)
                    menu = page.locator('[data-mention-section-id]:visible, [role="menu"]:visible')
                    if await self._click_visible_text(menu, pattern):
                        await page.wait_for_timeout(300)
                        await self._require_research_tool(page)
                        return
        raise BrowserTaskError("当前 ChatGPT 页面没有找到 Deep Research 入口。")

    async def _start_research_plan_if_present(self, page: Page, receipt: SubmitReceipt | None = None) -> bool:
        """Compatibility entry point: a persisted start intent is mandatory."""
        if receipt is None or receipt.research_start_state != "requested":
            return False
        await self.start_research(receipt)
        return True

    async def start_research(self, receipt: SubmitReceipt) -> bool:
        """Consume exactly one already-persisted intent for the matched plan."""
        if receipt.research_start_state != "requested":
            raise BrowserTaskError("Research start requires a fresh persisted one-use intent; no action was repeated.")
        page = await self._conversation_page(receipt.conversation_url, create=False)
        clicked = False
        async with self._ui_transaction(page):
            if receipt.research_start_state != "requested":
                raise BrowserTaskError("Research start intent was already consumed.")
            surface = await self._read_turn_surface(page)
            if (surface.get("verification_required") or surface.get("login_required") or surface.get("blocking_dialog") or
                    _is_explicit_login_page(page.url)):
                raise BrowserTaskError("The browser is blocked; no research start action was taken.")
            output = _resolve_current_turn(receipt, surface["messages"])
            users = [message for message in surface["messages"] if message.get("role") == "user"]
            if (receipt.submission_state != "confirmed" or not receipt.current_user_message_id or not output or
                    not users or users[-1]["id"] != receipt.current_user_message_id or not any(
                        isinstance(text, str) and _prompt_text_sha256(text) == receipt.submitted_prompt_sha256
                        for text in [users[-1].get("prompt_text", users[-1].get("text", "")), *users[-1].get("prompt_text_candidates", [])])):
                raise BrowserTaskError("The current research plan could not be associated with the original latest user turn.")
            ids = list(dict.fromkeys(identity for identity in [receipt.current_user_message_id,
                receipt.current_assistant_message_id,
                *(identity for message in output for identity in [message["id"], *message.get("container_ids", [])])] if identity))
            run_ids = ([receipt.research_run_id] if receipt.research_run_id else
                       list(dict.fromkeys(identity for message in output for identity in message.get("research_run_ids", []))))
            panel_ids = list(dict.fromkeys(identity for message in output for identity in message.get("research_panel_ids", [])))
            targets, unassociated = await self._research_targets(page, current_message_ids=ids,
                current_run_ids=run_ids, current_panel_ids=panel_ids)
            if unassociated:
                raise BrowserTaskError("A research panel has unknown ownership; the plan was not started.")
            # A plan may be in a correlated iframe/portal or directly in the
            # current assistant message. Only the latter needs per-control ID
            # checking; generic Start is restricted to known research targets.
            candidates = [(target, is_frame, True) for target, is_frame, _metadata in targets] + [(page, False, False)]
            for target, is_frame, research_target in candidates:
                pattern = re.compile(r"^(?:开始|Start|开始研究|Start research|启动研究|开始深度研究)$" if research_target else
                                     r"^(?:开始研究|Start research|启动研究|开始深度研究)$", re.I)
                controls = target.get_by_role("button", name=pattern)
                for index in range(await controls.count()):
                    control = controls.nth(index)
                    if not await control.is_visible() or not await control.is_enabled():
                        continue
                    if not research_target:
                        metadata = await control.evaluate(RESEARCH_ELEMENT_METADATA)
                        if not isinstance(metadata, dict) or research_association(metadata, message_ids=set(ids),
                                run_ids=set(run_ids), panel_ids=set(panel_ids)) != "matched":
                            continue
                    receipt.research_start_state = "attempted"
                    submission_phase("research_start_attempted", user_message_id=receipt.current_user_message_id,
                                     assistant_message_id=receipt.current_assistant_message_id)
                    await control.click(timeout=min(2000, int(self.settings.browser_timeout_seconds * 1000)))
                    clicked = True
                    break
                if research_target and not clicked:
                    nested = await self._nested_research_control(target, is_frame, pattern)
                    if nested.get("count") == 1:
                        receipt.research_start_state = "attempted"
                        submission_phase("research_start_attempted", user_message_id=receipt.current_user_message_id,
                                         assistant_message_id=receipt.current_assistant_message_id, native_inner_document=True)
                        action = await self._nested_research_control(target, is_frame, pattern, click=True)
                        if not action.get("clicked"):
                            raise BrowserTaskError("The reserved native research control changed; do not repeat the start action.")
                        clicked = True
                if clicked:
                    break
            if not clicked:
                transitioned = await self._research_frame_surface(page, current_message_ids=ids,
                    current_run_ids=run_ids, current_panel_ids=panel_ids)
                if transitioned["matched"] and transitioned["state"] in {"running", "complete", "stopped"}:
                    # The page can consume its own plan between observation
                    # and action. Confirm that transition without clicking.
                    receipt.research_start_state = "confirmed"
                    receipt.research_observed = True
                    return True
                raise BrowserTaskError("The matched research plan is not ready; no Start button was clicked.")
        deadline = asyncio.get_running_loop().time() + min(8.0, self.settings.browser_timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self.inspect(receipt)
            if snapshot.research_observed and snapshot.research_state in {"running", "complete", "stopped"}:
                receipt.research_start_state = "confirmed"
                return True
            if snapshot.user_input_kind == "manual_browser":
                break
            await asyncio.sleep(min(.1, max(0, deadline - asyncio.get_running_loop().time())))
        raise BrowserTaskError("Research start action was consumed but not confirmed; observe the original conversation and do not repeat it.")

    async def _select_visible_model(self, page: Page, model: str) -> None:
        pattern = re.compile(re.escape(model), re.I)
        if await self._click_visible_text(page, pattern):
            return
        raise BrowserTaskError(f"当前页面找不到指定模型：{model}")
