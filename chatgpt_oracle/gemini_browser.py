"""Gemini Web adapter for a native, already authenticated local Chrome profile."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Page, async_playwright

from .browser import (
    BrowserCapabilities, BrowserCapacityError, BrowserComposerDraftPresent,
    BrowserNeedsUserAction, BrowserPageNotReady, BrowserSnapshot, BrowserTaskError, ChatGPTBrowser,
    ModelUnavailableError, SubmitReceipt, _pid_alive,
)
from .browser_resources import MAX_BROWSER_TABS, ProfileResources
from .config import Settings
from .file_store import FileRecord
from .gemini_dom import READ_COMPOSER_ATTACHMENTS, READ_SURFACE, READ_SELECTION_DIAGNOSTICS, READ_CONTROLLED_MENU, READ_SUBMISSION_STATE
from .submission_diagnostics import submission_phase
from .models import utc_now
from .oracle_protocol import normalized_text_sha256
from .providers import GEMINI, gemini_account_home, gemini_account_index, gemini_same_account
from .gemini_accounts import GeminiAccountState


class GeminiTaskError(BrowserTaskError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _Operation:
    session_id: str | None = None
    conversation_url: str | None = None
    pages: dict[Any, str] = field(default_factory=dict)
    unresponsive: bool = False
    opened_reports: set[str] = field(default_factory=set)
    selection_stage: str | None = None
    runtime_recovery_attempts: set[int] = field(default_factory=set)
    observation_only: bool = False
    account_index: int | None = None
    account_attempts: list[dict[str, Any]] = field(default_factory=list)
    account_switch: dict[str, Any] | None = None
    inputs_started: bool = False
    acknowledgement_started: float | None = None


def identify_messages(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Position-based user DOM ids are not identities; anchor text to prior reply."""
    messages = []
    previous_reply = "start"
    for item in raw:
        message = dict(item)
        if message["role"] == "user":
            digest = normalized_text_sha256(str(message.get("text") or ""))
            message["id"] = "gemini-user-" + hashlib.sha256(
                (previous_reply + ":" + digest).encode("utf-8")
            ).hexdigest()
        elif message.get("id"):
            previous_reply = message["id"]
        else:
            continue
        messages.append(message)
    return messages


def matched_turn(messages: list[dict[str, Any]], receipt: SubmitReceipt) -> tuple[dict | None, list[dict]]:
    """Match a turn with a content hash and an independently retained boundary.

    A prior assistant can temporarily unmount, changing the derived user ID.
    Never fall back to a globally unique new ID/hash alone: a historical repeat
    can acquire a new derived ID for exactly the same reason.
    """
    def has_prompt(message):
        return (message["role"] == "user" and receipt.submitted_prompt_sha256
                and normalized_text_sha256(message["text"]) == receipt.submitted_prompt_sha256)

    known_assistant = receipt.current_assistant_message_id
    if (receipt.research_expected and receipt.research_user_message_id
            and receipt.current_user_message_id != receipt.research_user_message_id
            and known_assistant == receipt.research_assistant_message_id):
        # Research reports belong to the provider's separate Start turn. They
        # are checked by its action receipt, not as replies to the initial user.
        known_assistant = None
    user = None
    if receipt.current_user_message_id:
        pinned = [m for m in messages if m["role"] == "user" and m["id"] == receipt.current_user_message_id]
        if pinned:
            if len(pinned) != 1 or not has_prompt(pinned[0]):
                return None, []
            user = pinned[0]
    if user is None and known_assistant:
        known = [i for i, m in enumerate(messages) if m["role"] == "assistant"
                 and m["id"] == known_assistant]
        if len(known) == 1 and known_assistant not in receipt.assistant_message_ids_before:
            prior = next((m for m in reversed(messages[:known[0]]) if m["role"] == "user"), None)
            if prior and has_prompt(prior) and prior['id'] not in receipt.user_message_ids_before:
                user = prior
    if user is None and receipt.assistant_message_ids_before:
        anchor = receipt.assistant_message_ids_before[-1]
        boundaries = [i for i, m in enumerate(messages) if m["role"] == "assistant" and m["id"] == anchor]
        if len(boundaries) == 1:
            after = messages[boundaries[0] + 1:]
            # Only the first adjacent user after the original baseline tail
            # can be the submitted turn. Never jump across another user/card.
            if (after and has_prompt(after[0]) and after[0]["id"] not in receipt.user_message_ids_before
                    and sum(bool(has_prompt(m)) for m in after) == 1):
                user = after[0]
    if user is None and not receipt.current_user_message_id and not receipt.assistant_message_ids_before and not receipt.user_message_ids_before:
        users = [m for m in messages if m["role"] == "user"]
        if len(users) == 1 and has_prompt(users[0]) and messages and messages[0] is users[0]:
            user = users[0]
    if user is None:
        return None, []
    start = next(i for i, item in enumerate(messages) if item is user)
    replies = []
    for item in messages[start + 1:]:
        if item["role"] == "user":
            break
        if item["role"] == "assistant":
            replies.append(item)
    if known_assistant and not any(m['id'] == known_assistant for m in replies):
        return None, []
    if known_assistant:
        # Adjacent regeneration/variant cards are not permission to replace an
        # already bound answer, even when the derived user identity collides.
        replies = [m for m in replies if m['id'] == known_assistant]
    return user, replies


def research_turn(messages: list[dict[str, Any]], receipt: SubmitReceipt) -> tuple[dict | None, list[dict]]:
    """A Start research click inserts its own provider user turn; preserve origin."""
    if receipt.research_start_state not in {"requested", "confirmed"}:
        return None, []
    if not receipt.research_user_message_id:
        original, plans = matched_turn(messages, receipt)
        if not original or not plans:
            return None, []
        plan_ids = {item["id"] for item in plans}
        candidates = []
        previous = None
        for item in messages:
            if (item["role"] == "user" and previous in plan_ids
                    and item["text"].strip().casefold() in {"开始研究", "启动研究", "start research", "begin research"}):
                candidates.append(item)
            if item["role"] == "assistant":
                previous = item["id"]
        if len(candidates) != 1:
            return None, []
        receipt.research_user_message_id = candidates[0]["id"]
        receipt.research_user_prompt_sha256 = normalized_text_sha256(candidates[0]["text"])
    action_receipt = replace(receipt, current_user_message_id=receipt.research_user_message_id,
                             current_assistant_message_id=receipt.research_assistant_message_id,
                             submitted_prompt_sha256=receipt.research_user_prompt_sha256)
    user, replies = matched_turn(messages, action_receipt)
    if user:
        receipt.research_start_state = "confirmed"
        if replies:
            receipt.research_assistant_message_id = replies[-1]["id"]
    return user, replies


class GeminiBrowser:
    requires_turn_receipts = True
    continuation_accepts_mode = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._browser = None
        self._context = None
        self._start_lock = asyncio.Lock()
        self._preparation_lock = asyncio.Lock()
        self._resources = ProfileResources(settings.browser_profile_dir,
                                           lock_timeout_seconds=settings.browser_timeout_seconds)
        self._current: ContextVar[_Operation | None] = ContextVar("gemini_operation", default=None)
        self._fallback = _Operation()
        self._native_connector = None
        self._accounts = GeminiAccountState(settings.jobs_dir / "account-preference.json")

    @property
    def _operation(self) -> _Operation:
        return self._current.get() or self._fallback

    @asynccontextmanager
    async def operation_scope(self, session_id=None, conversation_url=None):
        token = self._current.set(_Operation(session_id, conversation_url))
        try:
            yield
        finally:
            self._current.reset(token)

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser and self._browser.is_connected():
                return
            endpoint = self.settings.browser_cdp_url or ""
            parsed = urlparse(endpoint)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise GeminiTaskError("INVALID_CDP_ENDPOINT", "Gemini requires a loopback HTTP CDP endpoint")
            if not self.settings.shared_browser:
                self._playwright = await async_playwright().start()
            try:
                if self.settings.shared_browser:
                    # Reuse the common native-CDP connection/launch path. Its
                    # profile lock prevents a second Chrome launch; no ChatGPT
                    # navigation or model behavior is used by this connector.
                    self._native_connector = ChatGPTBrowser(self.settings)
                    await self._native_connector.start()
                    self._playwright = self._native_connector._playwright
                    self._browser = self._native_connector._browser
                    self._context = self._native_connector._context
                    return
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint, timeout=int(self.settings.browser_timeout_seconds * 1000))
                if len(self._browser.contexts) != 1:
                    raise BrowserTaskError("The dedicated Gemini browser must have one context")
                self._context = self._browser.contexts[0]
            except Exception as exc:
                await self.close()
                raise GeminiTaskError("BROWSER_NOT_READY", "Native Gemini Chrome CDP is unavailable; run scripts/setup-gemini-browser.ps1 and sign in manually") from exc

    async def close(self) -> None:
        if self._native_connector is not None:
            connector, self._native_connector = self._native_connector, None
            await connector.close()
            self._playwright = self._browser = self._context = None
            return
        playwright = self._playwright
        self._playwright = self._browser = self._context = None
        if playwright:
            await playwright.stop()  # Disconnect the driver, never close the user's native Chrome.

    def mark_unresponsive(self):
        self._operation.unresponsive = True

    async def abort_driver(self):
        await self.close()

    async def discard_unresponsive_pages(self):
        await self.start()
        current_pages = await self._pages_by_target()
        self._operation.pages = {current_pages[target]: target for target in self._operation.pages.values()
                                 if target in current_pages}
        self._operation.unresponsive = False
        await self.close_owned_pages(keep_for_user=False)

    async def _target_id(self, page: Page) -> str:
        connection = await self._context.new_cdp_session(page)
        try:
            return (await connection.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        finally:
            await connection.detach()

    async def _pages_by_target(self) -> dict[str, Page]:
        result = {}
        for page in self._context.pages:
            if not page.is_closed():
                result[await self._target_id(page)] = page
        return result

    async def _account_order(self) -> list[int]:
        configured = self.settings.gemini_account_indexes
        preferred = self.settings.gemini_preferred_account_index
        async with self._resources.locked() as conn:
            state = self._accounts.read()
            selected = self._accounts.explicit_selection(state)
            if selected is not None:
                index = selected["account_index"]
                if index not in configured:
                    raise GeminiTaskError("ACCOUNT_SELECTION_UNAVAILABLE", "The explicitly selected Gemini account is no longer configured; select another account or reset the preference")
                return [index] if selected["strict"] else list(dict.fromkeys([index, *configured]))
            if self._context is None:
                return list(dict.fromkeys(([preferred] if preferred is not None else []) + list(configured)))
            known = state.get("observed_routes", {})
            pages = await self._pages_by_target()
            records = {row[0]: row[1] for row in conn.execute("SELECT target_id,owner_pid FROM pages")}
            observed = {}
            manual = []
            for target, page in pages.items():
                try:
                    index = gemini_account_index(page.url)
                except ValueError:
                    continue
                observed[target] = index
                # A broker-created page is recorded when navigated. A new
                # unmanaged tab, or an idle page whose account changed since
                # then, is evidence of a manual account selection.
                if (index in configured and records.get(target) is None
                        and (target not in known and target not in records
                             or target in known and known[target] != index)):
                    manual.append(index)
            state["observed_routes"] = observed
            if preferred is None and manual:
                preferred = manual[-1]
                state["preferred_index"] = preferred
                state["selection_source"] = "manual_browser_route"
            if preferred is None:
                candidate = state.get("preferred_index")
                preferred = candidate if type(candidate) is int and candidate in configured else configured[0]
            self._accounts.write(state)
        return list(dict.fromkeys([preferred, *configured]))

    async def select_account(self, account_index: int | None, strict: bool = True) -> dict:
        """Persist a next-session routing choice without opening or moving pages."""
        configured = self.settings.gemini_account_indexes
        if account_index is not None and type(account_index) is not int:
            raise GeminiTaskError("INVALID_ACCOUNT_INDEX", "account_index must be an integer or null; booleans are not account indices")
        if account_index is not None and account_index not in configured:
            raise GeminiTaskError("INVALID_ACCOUNT_INDEX", "account_index must be one of the configured Gemini account indices")
        if type(strict) is not bool:
            raise GeminiTaskError("INVALID_ACCOUNT_SELECTION", "strict must be a boolean")
        async with self._resources.locked():
            self._accounts.select(account_index, strict=strict, selected_at=utc_now())
        explicit = account_index is not None
        return {
            "account_index": account_index,
            "home_url": gemini_account_home(account_index) if explicit else None,
            "configured_indexes": list(configured),
            "mode": "explicit" if explicit else "automatic",
            "strict": strict if explicit else False,
            "fallback_allowed": not strict if explicit else True,
            "applies_to": "new_sessions",
            "existing_sessions_unchanged": True,
            "context_rebuild_action": "compact_or_reassemble_context_then_submit_new_session",
            "automatic_replay": False,
            "account_availability_verified": False,
        }

    async def _prefer_account(self, index: int, *, page=None, reason="verified_selection") -> None:
        async with self._resources.locked():
            state = self._accounts.read()
            state.update(preferred_index=index, selection_source=reason, updated_at=utc_now())
            if page is not None and self._context is not None:
                state.setdefault("observed_routes", {})[await self._target_id(page)] = index
            self._accounts.write(state)

    def _account_attempt(self, index, state, *, cause=None, available=None):
        value = {"account_index": index, "home_url": gemini_account_home(index),
                 "state": state, "observed_at": utc_now()}
        if cause:
            value["reason"] = cause
        if available is not None:
            value["available_models"] = list(available)
        previous = next((i for i, item in enumerate(self._operation.account_attempts)
                         if item["account_index"] == index), None)
        if previous is None:
            self._operation.account_attempts.append(value)
        else:
            self._operation.account_attempts[previous] = value
        submission_phase("account_observed", **value)

    def _account_error(self, exc):
        exc.account_index = self._operation.account_index
        exc.account_attempts = list(self._operation.account_attempts)
        exc.account_switch = self._operation.account_switch
        return exc

    @staticmethod
    def _is_account_availability_error(exc):
        return isinstance(exc, ModelUnavailableError) or getattr(exc, "code", None) in {
            "UPSTREAM_LIMIT", "EFFORT_UNAVAILABLE", "RESEARCH_UNAVAILABLE",
        }

    @asynccontextmanager
    async def _account_probe(self, index):
        """A temporary empty probe counts against the shared four-page limit."""
        await self.start()
        async with self._resources.locked() as conn:
            pages = await self._pages_by_target()
            if len(pages) >= MAX_BROWSER_TABS:
                raise BrowserCapacityError("Gemini account probe is waiting for a free shared browser page")
            page = await self._context.new_page()
            target = await self._target_id(page)
            conn.execute("INSERT INTO pages(target_id,owner_pid,retained,session_primary) VALUES(?,?,0,0)",
                         (target, os.getpid()))
            self._operation.pages[page] = target
        try:
            await page.goto(gemini_account_home(index), wait_until="domcontentloaded",
                            timeout=int(self.settings.browser_timeout_seconds * 1000))
            await self._ready(page)
            if gemini_account_index(page.url) != index:
                raise GeminiTaskError("ACCOUNT_ROUTE_MISMATCH", "Google redirected the account probe to another account; nothing was sent")
            yield page
        finally:
            async with self._resources.locked() as conn:
                row = conn.execute("SELECT owner_pid FROM pages WHERE target_id=?", (target,)).fetchone()
                if row and row[0] == os.getpid():
                    await self._close_page(page)
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target,))
                self._operation.pages.pop(page, None)

    @asynccontextmanager
    async def _account_read_page(self, index):
        await self.start()
        async with self._resources.locked() as conn:
            pages = await self._pages_by_target()
            owners = {row[0]: row[1] for row in conn.execute("SELECT target_id,owner_pid FROM pages")}
            candidates = []
            for target, page in pages.items():
                try:
                    if gemini_account_index(page.url) == index and owners.get(target) is None:
                        candidates.append(page)
                except ValueError:
                    pass
        if candidates:
            yield candidates[-1]
        else:
            async with self._account_probe(index) as page:
                yield page

    async def _close_page(self, page: Page):
        if page.is_closed():
            return
        if len([p for p in self._context.pages if not p.is_closed()]) <= 1:
            await page.goto("about:blank")
        else:
            await page.close()

    @staticmethod
    def _same_conversation(left, right):
        try:
            left = GEMINI.canonical_url(left)
            right = GEMINI.canonical_url(right)
            return gemini_same_account(left, right) and urlparse(left).path.split('/')[-1] == urlparse(right).path.split('/')[-1]
        except (ValueError, TypeError):
            return False

    async def _managed_page(self, conversation_url: str | None = None) -> Page:
        await self.start()
        if conversation_url:
            conversation_url = GEMINI.canonical_url(conversation_url)
            self._operation.account_index = gemini_account_index(conversation_url)
        elif self._operation.account_index is None:
            self._operation.account_index = (await self._account_order())[0]
        reused_page = False
        async with self._resources.locked() as conn:
            pages = await self._pages_by_target()
            records = list(conn.execute("SELECT target_id,owner_pid,retained,retain_key,session_key FROM pages"))
            if conversation_url:
                records.sort(key=lambda row: not (row[0] in pages and self._same_conversation(pages[row[0]].url, conversation_url)))
            for target, owner, retained, key, session in records:
                if target not in pages:
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target,))
                    continue
                if owner and owner != os.getpid() and not _pid_alive(owner):
                    conn.execute("UPDATE pages SET owner_pid=NULL WHERE target_id=?", (target,))
                    owner = None
                wanted = session == self._operation.session_id if self._operation.session_id else (
                    session is None and conversation_url is not None and self._same_conversation(pages[target].url, conversation_url))
                if wanted and (session is not None or conversation_url):
                    if owner not in {None, os.getpid()}:
                        raise BrowserCapacityError("Gemini Session page is owned by another active operation")
                    page = pages[target]
                    reused_page = True
                    self._operation.pages[page] = target
                    conn.execute("UPDATE pages SET owner_pid=?,retained=0 WHERE target_id=?", (os.getpid(), target))
                    break
            else:
                if self._operation.observation_only:
                    raise GeminiTaskError("TURN_NOT_VISIBLE", "The original Gemini page is unavailable; no page or question was created")
                # Never adopt the human's existing conversation or login page.
                registered = {row[0] for row in records}
                page = next((p for target, p in pages.items() if target not in registered and p.url == "about:blank"), None)
                if page is None:
                    if len(pages) >= MAX_BROWSER_TABS:
                        raise BrowserCapacityError("Gemini profile has reached its four-tab budget")
                    page = await self._context.new_page()
                target = await self._target_id(page)
                self._operation.pages[page] = target
                conn.execute("INSERT OR REPLACE INTO pages(target_id,owner_pid,retained,retain_key,session_key,session_primary) VALUES(?,?,0,?,?,1)",
                             (target, os.getpid(), self._operation.session_id, self._operation.session_id))
        if reused_page and not conversation_url and page.url != "about:blank":
            # A Session may already own an unsent page without a formal URL.
            # Changing the preference applies to newly allocated Sessions;
            # navigating this existing page would destroy its original draft.
            try:
                self._operation.account_index = gemini_account_index(page.url)
            except ValueError as exc:
                if urlparse(page.url).hostname == "accounts.google.com":
                    await self._ready(page)
                raise GeminiTaskError("ACCOUNT_ROUTE_CHANGED", "The retained Gemini Session page left its account route; its current page and drafts were preserved") from exc
            destination = page.url
        else:
            destination = conversation_url or gemini_account_home(self._operation.account_index)
        route_matches = self._same_conversation(page.url, destination) if conversation_url else page.url.rstrip('/') == destination.rstrip('/')
        if not route_matches:
            if conversation_url and reused_page and page.url != 'about:blank':
                if urlparse(page.url).hostname == 'accounts.google.com':
                    await self._ready(page)
                try:
                    changed_account = not gemini_same_account(page.url, conversation_url)
                except ValueError:
                    changed_account = False
                raise GeminiTaskError('ACCOUNT_ROUTE_CHANGED' if changed_account else 'CONVERSATION_ROUTE_MISMATCH',
                    'The retained page was manually moved away from its original conversation; its current page and drafts were preserved')
            if self._operation.observation_only:
                if (urlparse(page.url).hostname == 'accounts.google.com'
                        or urlparse(page.url).hostname == 'gemini.google.com' and not await self._logged_in(page)):
                    await self._ready(page)
                raise GeminiTaskError("GEMINI_RECOVERY_ROUTE_MISMATCH", "The owned Gemini page left the original conversation; it was not navigated")
            await page.goto(destination, wait_until="domcontentloaded",
                            timeout=int(self.settings.browser_timeout_seconds * 1000))
        try:
            same_account = gemini_same_account(page.url, destination)
        except ValueError:
            await self._ready(page)  # A real login redirect keeps its usual handoff.
            same_account = False
        if not same_account:
            raise GeminiTaskError("ACCOUNT_ROUTE_MISMATCH", "Gemini opened a different account; nothing was sent")
        if conversation_url:
            try:
                same_conversation = self._same_conversation(page.url, conversation_url)
            except ValueError:
                same_conversation = False
            if not same_conversation:
                raise GeminiTaskError("CONVERSATION_ROUTE_MISMATCH", "Gemini did not open the original account conversation; nothing was sent")
        else:
            await self._prefer_account(self._operation.account_index, page=page, reason="selected_route")
        return page

    async def _observation_page(self, conversation_url):
        previous = self._operation.observation_only
        self._operation.observation_only = True
        try:
            return await self._managed_page(conversation_url)
        finally:
            self._operation.observation_only = previous

    async def close_owned_pages(self, *, keep_for_user=False, retain_key=None):
        if not self._operation.pages:
            return
        await self.start()
        async with self._resources.locked() as conn:
            for page, target in list(self._operation.pages.items()):
                row = conn.execute("SELECT owner_pid FROM pages WHERE target_id=?", (target,)).fetchone()
                if not row or row[0] != os.getpid():
                    continue
                if keep_for_user and not page.is_closed():
                    conn.execute("UPDATE pages SET owner_pid=NULL,retained=1,retain_key=? WHERE target_id=?", (retain_key, target))
                else:
                    await self._close_page(page)
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target,))
            self._operation.pages.clear()

    async def _retained_pages(self, session_id=None, conversation_url=None, *, close=False):
        await self.start()
        async with self._resources.locked() as conn:
            pages = await self._pages_by_target()
            found = False
            for target, owner, key, session in list(conn.execute("SELECT target_id,owner_pid,retain_key,session_key FROM pages")):
                page = pages.get(target)
                if page is None:
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target,))
                    continue
                if session_id is not None and session_id not in {key, session}:
                    continue
                if conversation_url is not None and not self._same_conversation(page.url, conversation_url):
                    continue
                found = True
                if close:
                    if self._operation.session_id is not None and session != self._operation.session_id:
                        continue
                    if owner not in {None, os.getpid()} and _pid_alive(owner):
                        raise BrowserCapacityError("Gemini page is still in use")
                    await self._close_page(page)
                    conn.execute("DELETE FROM pages WHERE target_id=?", (target,))
            return found

    async def has_session_page(self, session_id):
        return await self._retained_pages(session_id=session_id)

    async def has_conversation_tab(self, conversation_url):
        return await self._retained_pages(conversation_url=GEMINI.canonical_url(conversation_url))

    async def close_retained_pages(self, retain_key):
        return await self._retained_pages(session_id=retain_key, close=True)

    async def close_conversation_tabs(self, conversation_url):
        return await self._retained_pages(conversation_url=GEMINI.canonical_url(conversation_url), close=True)

    async def _diagnostic_page(self):
        await self.start()
        return next((page for page in self._context.pages if urlparse(page.url).hostname == "gemini.google.com"), None) or await self._managed_page()

    async def browser_status(self):
        await self.start()
        index = (await self._account_order())[0]
        async with self._account_read_page(index) as page:
            logged_in = await self._logged_in(page)
            return {"provider": "gemini", "logged_in": logged_in, "url": page.url,
                    "account_index": index, "configured_account_indexes": list(self.settings.gemini_account_indexes),
                    "profile_dir": str(self.settings.browser_profile_dir), "cdp_connected": True}

    async def _logged_in(self, page):
        if urlparse(page.url).hostname != "gemini.google.com":
            return False
        login = page.get_by_role("button", name=re.compile(r"^(Sign in|登录)$", re.I))
        return not any([await login.nth(i).is_visible() for i in range(await login.count())])

    async def _ready(self, page):
        if urlparse(page.url).hostname != "gemini.google.com" or not await self._logged_in(page):
            raise BrowserNeedsUserAction("login_required", "请在专用原生 Chrome 中完成 Google 登录后继续。", code="LOGIN_REQUIRED")
        if await page.locator('iframe[src*="recaptcha"]:visible').count():
            raise BrowserNeedsUserAction("verification_required", "请在专用浏览器人工完成 Google 验证。", code="VERIFICATION_REQUIRED")
        composer = page.locator('rich-textarea .ql-editor[contenteditable="true"]').first
        await composer.wait_for(state="visible", timeout=int(self.settings.browser_timeout_seconds * 1000))
        return composer

    async def _draft(self, page, expected: str | None = None):
        composer = await self._ready(page)
        text = (await composer.inner_text()).strip()
        if normalized_text_sha256(text) != normalized_text_sha256(expected or ""):
            raise BrowserNeedsUserAction("composer_draft_present", "Gemini 输入框已有草稿，服务未覆盖。请保存或处理后再明确提交。",
                                        user_input_kind="composer_draft", code="COMPOSER_DRAFT_PRESENT")
        return composer

    async def _surface(self, page):
        raw = await page.evaluate(READ_SURFACE)
        raw["messages"] = identify_messages(raw["messages"])
        return raw

    def _identity_diagnostics(self, surface, receipt, phase):
        submission_phase(phase, current_user_message_id=receipt.current_user_message_id,
                         current_assistant_message_id=receipt.current_assistant_message_id,
                         submitted_prompt_sha256=receipt.submitted_prompt_sha256,
                         messages=[{"order": i, "role": item["role"], "id": item.get("id"),
                                    "dom_id": item.get("dom_id"), "container_id": item.get("container_id"),
                                    "text_sha256": normalized_text_sha256(str(item.get("text") or ""))}
                                   for i, item in enumerate(surface.get("messages", []))][-64:])

    async def _observe_turn_identity(self, page, receipt):
        """A transient renderer gap gets bounded reads of this same page."""
        budget = min(2.0, max(.05, self.settings.browser_timeout_seconds / 4))
        surface = {"messages": [], "active": False, "alerts": []}
        try:
            async with asyncio.timeout(budget):
                while True:
                    surface = await self._surface(page)
                    user, replies = matched_turn(surface["messages"], receipt)
                    if user:
                        return surface, user, replies
                    await asyncio.sleep(.1)
        except TimeoutError:
            self._identity_diagnostics(surface, receipt, "gemini_turn_identity_missing")
            return surface, None, []

    @staticmethod
    def _identity_pending_snapshot(page, receipt, surface):
        return BrowserSnapshot("RECOVERABLE_ERROR", page.url,
                               progress="Gemini 原回合暂未出现在页面；继续核查原会话，不重发问题。",
                               runtime_error_code="GEMINI_TURN_IDENTITY_MISSING",
                               current_user_message_id=receipt.current_user_message_id,
                               current_assistant_message_id=receipt.current_assistant_message_id,
                               submission_state=receipt.submission_state,
                               submission_verified_at=receipt.submission_verified_at,
                               generation_active=bool(surface.get("active")))

    def _picker(self, page):
        return page.get_by_role("button", name=re.compile(r"Open mode picker|打开模式选择器", re.I)).first

    @staticmethod
    def _model_name(label: str) -> str | None:
        match = re.search(r"(?:\d+(?:\.\d+)?\s+)?(?:Flash-Lite|Flash|Pro|Argon)\b", label, re.I)
        return match.group(0) if match else None

    def _selection_phase(self, phase, **details):
        self._operation.selection_stage = phase
        submission_phase(phase, **details)

    async def _selection_failure(self, page, exc):
        try:
            async with asyncio.timeout(1):
                surface = await page.evaluate(READ_SELECTION_DIAGNOSTICS)
        except Exception:
            surface = {"unavailable": True}
        submission_phase("selection_failed", stage=self._operation.selection_stage,
                         exception_type=type(exc).__name__, surface=surface)

    async def _until(self, check, description):
        try:
            async with asyncio.timeout(self.settings.browser_timeout_seconds):
                while True:
                    result = await check()
                    if result:
                        return result
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise GeminiTaskError("SELECTION_NOT_VERIFIED", description) from exc

    async def _dismiss_discovery(self, page):
        backdrops = page.locator('.cdk-overlay-backdrop:visible')
        if not await backdrops.count():
            return
        panes = page.locator('.cdk-overlay-pane:visible')
        known = page.locator('.cdk-overlay-pane:visible:has(gem-discovery-tooltip)')
        if (await backdrops.count() != 1 or not await panes.count()
                or await panes.count() != await known.count()
                or await page.locator('[role="dialog"]:visible').count()):
            raise BrowserNeedsUserAction("Gemini UI overlay requires attention",
                                        "Gemini 有未识别的弹窗遮挡交互，请人工处理后继续。",
                                        code="GEMINI_UI_BLOCKED")
        # The observed introduction dismisses via its normal backdrop action.
        # Never force-click through an overlay or delete its DOM.
        await backdrops.click(timeout=int(self.settings.browser_timeout_seconds * 1000))
        await backdrops.wait_for(state="hidden", timeout=int(self.settings.browser_timeout_seconds * 1000))
        submission_phase("discovery_dismissed")

    async def _controlled_menu(self, page, trigger, *, ensure_open=False, item_pattern=None):
        # Cold Angular pages replace both triggers and aria-controls during
        # hydration. Never keep waiting on the first (possibly still visible,
        # empty) menu. Observe the current relationship on every pass.
        attempts = 0
        last_click = float('-inf')
        previous_control = None

        async def ready_menu():
            nonlocal attempts, last_click, previous_control
            current = await trigger() if callable(trigger) else trigger
            state = await current.evaluate(READ_CONTROLLED_MENU)
            identifier = state['controls']
            if identifier != previous_control:
                submission_phase("menu_control_observed", controls=identifier,
                                 previous_controls=previous_control)
                previous_control = identifier
            rows_ready = any(not item_pattern or item_pattern.search(label) for label in state['items'])
            if state['visible'] and state['expanded'] and state['menu_visible'] and rows_ready:
                return page.locator('[id=' + json.dumps(identifier) + ']')
            now = asyncio.get_running_loop().time()
            if (ensure_open and state['visible'] and not state['disabled'] and not state['expanded']
                    and attempts < 3 and now - last_click >= .25):
                if await page.locator('[role="dialog"]:visible').count():
                    raise BrowserNeedsUserAction("Gemini dialog appeared during selection",
                                                "Gemini 弹窗需要人工处理；服务没有关闭它。", code="GEMINI_UI_BLOCKED")
                # A bounded normal click reopens only a collapsed menu. It
                # neither selects a different model nor retries a submission.
                await current.click(timeout=int(self.settings.browser_timeout_seconds * 1000))
                attempts += 1
                last_click = asyncio.get_running_loop().time()
                submission_phase("menu_open_requested", controls=identifier, attempt=attempts)
            return None

        return await self._until(ready_menu, "Gemini current controlled menu did not become ready")

    async def _open_picker(self, page):
        await self._dismiss_discovery(page)
        trigger = self._picker(page)
        await trigger.wait_for(state="visible", timeout=int(self.settings.browser_timeout_seconds * 1000))
        return await self._controlled_menu(page, trigger, ensure_open=True)

    async def _close_picker(self, page):
        trigger = self._picker(page)
        for _ in range(3):
            identifier = await trigger.get_attribute("aria-controls")
            if not identifier:
                return
            state = await page.evaluate("""id => {
              const root=document.getElementById(id);
              return Array.from(document.querySelectorAll('[role="menu"]'))
                .filter(e=>e.getClientRects().length && (e===root || root?.contains(e))).map(e=>e.id);
            }""", identifier)
            if not state and await trigger.get_attribute("aria-expanded") != "true":
                return
            if await page.locator('[role="dialog"]:visible').count():
                raise BrowserNeedsUserAction("Gemini dialog appeared during selection",
                                            "Gemini 弹窗需要人工处理；服务没有关闭它。", code="GEMINI_UI_BLOCKED")
            if not state:
                async def closed():
                    return await trigger.get_attribute("aria-expanded") != "true"
                await self._until(closed, "Gemini picker closing state did not settle")
                return
            await page.keyboard.press("Escape")
            # Wait for this layer to close before sending the next Escape.
            await page.wait_for_function("""before => before.some(id => {
              const e=document.getElementById(id);return !e || !e.getClientRects().length;
            })""", arg=state, timeout=int(self.settings.browser_timeout_seconds * 1000))
        raise GeminiTaskError("SELECTION_NOT_VERIFIED", "Gemini picker did not close")

    @staticmethod
    async def _read_menu(menu):
        return await menu.evaluate("""root => Array.from(root.querySelectorAll('[role="menuitem"]'))
          .filter(e=>e.getClientRects().length && e.closest('[role="menu"]')===root)
          .map(e=>({label:e.innerText.trim(), selected:e.classList.contains('selected') || e.getAttribute('aria-checked')==='true',
                    disabled:e.getAttribute('aria-disabled')==='true' || !!e.disabled}))""")

    async def _thinking_menu(self, page, menu):
        self._selection_phase("effort_menu_opening")

        async def current_thinking():
            # The parent can remount too; resolve it through today's picker,
            # rather than retaining the root returned before hydration.
            current_menu = await self._open_picker(page)
            thinking = current_menu.get_by_role("menuitem", name=re.compile(r"^(Thinking level|思考级别|推理强度)$", re.I))
            await thinking.wait_for(state="visible", timeout=int(self.settings.browser_timeout_seconds * 1000))
            return thinking

        return await self._controlled_menu(page, current_thinking, ensure_open=True,
                                           item_pattern=re.compile(r"\b(Low|Medium|High)\b", re.I))

    @staticmethod
    def _same_model(left, right):
        return re.sub(r"^[\d.]+\s+", "", left or "").casefold() == re.sub(r"^[\d.]+\s+", "", right or "").casefold()

    async def _wait_picker_model(self, page, wanted):
        async def matches():
            label = await self._picker(page).get_attribute("aria-label") or ""
            return self._same_model(self._model_name(label), wanted)
        await self._until(matches, "Gemini model label did not settle after selection")

    async def _options(self, page, *, include_effort=True):
        self._selection_phase("model_menu_reading")
        failure = None
        try:
            menu = await self._open_picker(page)
            async def model_rows():
                nonlocal menu
                menu = await self._open_picker(page)
                rows = await self._read_menu(menu)
                options = [{"name": self._model_name(i["label"]), "disabled": i["disabled"]}
                           for i in rows if self._model_name(i["label"])]
                selected = [self._model_name(i["label"]) for i in rows if i["selected"] and self._model_name(i["label"])]
                if options and len(selected) == 1:
                    label = await self._picker(page).get_attribute("aria-label") or ""
                    if self._same_model(selected[0], self._model_name(label)):
                        return options, selected[0]
            options, current = await self._until(model_rows, "Gemini selected model could not be read consistently")
            submission_phase("model_observed", model=current)
            efforts = []
            selected_effort = None
            thinking = menu.get_by_role("menuitem", name=re.compile(r"^(Thinking level|思考级别|推理强度)$", re.I))
            if include_effort and (self._same_model(current, "Argon") or await thinking.count()):
                child = await self._thinking_menu(page, menu)
                for item in await self._read_menu(child):
                    label = item["label"]
                    match = re.search(r"\b(Low|Medium|High)\b", label, re.I)
                    if match and not item["disabled"]:
                        effort = {"label": match.group(1).title(), "value": len(efforts)}
                        efforts.append(effort)
                        if item["selected"]:
                            selected_effort = effort
            return options, current, efforts, selected_effort
        except BaseException as exc:
            failure = exc
            await self._selection_failure(page, exc)
            raise
        finally:
            try:
                async with asyncio.timeout(min(2, self.settings.browser_timeout_seconds)):
                    await self._close_picker(page)
            except Exception as exc:
                if failure is None:
                    await self._selection_failure(page, exc)
                    raise

    async def capabilities(self):
        await self.start()
        async with self._resources.ui_locked():
            index = (await self._account_order())[0]
            async with self._account_read_page(index) as page:
                await self._ready(page)
                await page.bring_to_front()
                options, current, efforts, selected = await self._options(page)
                return BrowserCapabilities([item["name"] for item in options if not item["disabled"]],
                                           current, efforts, selected,
                                           {"low": "Low", "medium": "Medium", "high": "High"},
                                           page.url, True, account_index=index)

    async def _selection(self, page, model, quality, reasoning_effort, allow_downgrade):
        wanted = (model or "Argon").strip()
        effort = (reasoning_effort or (quality if quality != "auto" else "high")).title()
        if effort not in {"Low", "Medium", "High"}:
            raise GeminiTaskError("INVALID_REASONING_EFFORT", "Gemini thinking level must be low, medium or high")
        options, current, _, _ = await self._options(page, include_effort=False)
        available = [item["name"] for item in options if not item["disabled"]]
        selected = next((name for name in available if name.casefold() == wanted.casefold()
                         or name.casefold().endswith(" " + wanted.casefold())), None)
        if selected is None:
            # Absence in the first hydrated menu is not yet a quota verdict.
            # Re-read this account's picker before considering another account.
            deadline = asyncio.get_running_loop().time() + min(2, self.settings.browser_timeout_seconds / 4)
            while selected is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.1)
                options, current, _, _ = await self._options(page, include_effort=False)
                available = [item["name"] for item in options if not item["disabled"]]
                selected = next((name for name in available if self._same_model(name, wanted)), None)
        if selected is None:
            if not allow_downgrade or not available:
                raise ModelUnavailableError(wanted, available, "Requested Gemini model is unavailable; nothing was sent")
            selected = next((name for alias in ("Pro", "Flash", "Flash-Lite") for name in available
                             if name.endswith(alias)), available[0])
        try:
            async with asyncio.timeout(2 * self.settings.browser_timeout_seconds):
                if current != selected:
                    self._selection_phase("model_selecting")
                    menu = await self._open_picker(page)
                    await menu.get_by_role("menuitem", name=re.compile(re.escape(selected), re.I)).first.click()
                    await self._wait_picker_model(page, selected)
                    await self._close_picker(page)
                _, actual_model, efforts, actual_effort = await self._options(page)
                if actual_model != selected:
                    raise GeminiTaskError("SELECTION_NOT_VERIFIED", "Gemini model did not match the requested selection")
                self._selection_phase("model_verified", model=actual_model)
                if effort not in {item["label"] for item in efforts}:
                    raise GeminiTaskError("EFFORT_UNAVAILABLE", "Requested Gemini thinking level is unavailable; nothing was sent")
                if not actual_effort or actual_effort["label"] != effort:
                    menu = await self._open_picker(page)
                    child = await self._thinking_menu(page, menu)
                    self._selection_phase("effort_selecting")
                    await child.get_by_role("menuitem", name=re.compile(r"\b" + effort + r"\b", re.I)).click()
                    await self._close_picker(page)
                    _, actual_model, _, actual_effort = await self._options(page)
                if actual_model != selected or not actual_effort or actual_effort["label"] != effort:
                    raise GeminiTaskError("SELECTION_NOT_VERIFIED", "Gemini selection could not be read back; nothing was sent")
                self._selection_phase("effort_verified", effort=effort)
                return {"resolved_model": actual_model, "resolved_reasoning_effort": effort,
                        "selection_source": "visible_picker", "selection_verified_at": utc_now()}
        except BaseException as exc:
            await self._selection_failure(page, exc)
            try:
                async with asyncio.timeout(min(2, self.settings.browser_timeout_seconds)):
                    await self._close_picker(page)
            except Exception:
                pass
            raise

    async def _verify_selection(self, page, selection):
        _, model, _, effort = await self._options(page)
        if model != selection["resolved_model"] or not effort or effort["label"] != selection["resolved_reasoning_effort"]:
            raise GeminiTaskError("MODEL_CHANGED", "Gemini model/effort changed after preparation; message was not sent")

    async def _set_research(self, page, active: bool):
        selected = page.get_by_role("button", name=re.compile(r"Deselect Deep research|取消选择.*研究", re.I))
        if await selected.count():
            if not active:
                await selected.click()
            return
        if active:
            await self._open_upload_menu(page)
            control = page.get_by_role("menuitemcheckbox", name=re.compile(r"Deep research|深度研究", re.I))
            await control.wait_for(state="visible", timeout=int(self.settings.browser_timeout_seconds * 1000))
            if not await control.is_enabled():
                await page.keyboard.press("Escape")
                raise GeminiTaskError("RESEARCH_UNAVAILABLE", "Gemini Deep Research is unavailable for this account")
            await control.click()

    async def _open_upload_menu(self, page):
        """Open the upload trigger once, without fighting attachment tooltips.

        Moving directly from a preview onto the trigger can keep Material's
        filename tooltip hovered over that trigger indefinitely. Move out of
        both first; if a focus tooltip remains, use the same button's native
        keyboard action. Neither path changes the composer or its attachments.
        """
        trigger = page.get_by_role("button", name=re.compile(r"^Upload & tools$|上传和工具|上传与工具", re.I))
        timeout = min(5.0, self.settings.browser_timeout_seconds)

        async def opened():
            controls = await trigger.get_attribute("aria-controls")
            if controls:
                root = page.locator('[id=' + json.dumps(controls) + ']:visible')
                return bool(await root.locator('[role="menuitem"]:visible, [role="menuitemcheckbox"]:visible').count())
            if await trigger.get_attribute("aria-expanded") == "true":
                # Some production triggers have no aria-controls. Do not
                # mistake a stale model menu for the upload menu in that case.
                items = page.get_by_role("menuitem", name=re.compile(r"Upload files?|上传文件|上传图片|从计算机|添加照片", re.I))
                for item in await items.all():
                    if await item.is_visible():
                        return True
            return False

        try:
            async with asyncio.timeout(timeout):
                await trigger.wait_for(state="visible")
                if await opened():
                    return
                # An expanded trigger with delayed menu content must not be
                # clicked again: that would close the menu before it hydrates.
                if await trigger.get_attribute("aria-expanded") != "true":
                    await page.mouse.move(0, 0)
                    tooltips = page.locator('.mat-mdc-tooltip-surface:visible, .mdc-tooltip__surface:visible, [role="tooltip"]:visible')
                    hide_deadline = asyncio.get_running_loop().time() + min(.4, timeout / 3)
                    while await tooltips.count() and asyncio.get_running_loop().time() < hide_deadline:
                        await asyncio.sleep(.05)
                    if await tooltips.count():
                        # Keyboard is appropriate only for a passive tooltip,
                        # never for bypassing an unrelated modal/backdrop.
                        blocking = page.locator('[role="dialog"]:visible, [aria-modal="true"]:visible, .cdk-overlay-backdrop:visible')
                        if await blocking.count():
                            raise GeminiTaskError("UPLOAD_MENU_BLOCKED", "Gemini upload control is covered by a dialog; current draft was preserved")
                        await trigger.focus()
                        await trigger.press("Enter")
                    else:
                        await trigger.click()
                while not await opened():
                    await asyncio.sleep(.05)
        except TimeoutError as exc:
            raise GeminiTaskError("UPLOAD_MENU_UNAVAILABLE", "Gemini upload menu did not become ready; current draft and attachments were preserved") from exc

    async def _upload(self, page, uploads):
        from pathlib import Path
        from .uploads import UploadError

        if len(uploads) > 10:
            raise GeminiTaskError("TOO_MANY_FILES", "Gemini accepts at most 10 files")
        for item in uploads:
            metadata = item if isinstance(item, FileRecord) else item.metadata
            GEMINI.validate_file(metadata.filename, metadata.mime_type, metadata.size)
        if not uploads:
            return []
        baseline = await page.evaluate(READ_COMPOSER_ATTACHMENTS)
        self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        verified = []
        images = 0
        for index, item in enumerate(uploads):
            metadata = item if isinstance(item, FileRecord) else item.metadata
            image = metadata.mime_type.startswith("image/") or Path(metadata.filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
            if isinstance(item, FileRecord) and Path(item.path).name == metadata.filename:
                # CDP may read file bytes after the preview card appears. The
                # task-pinned FileStore path outlives the whole send/answer;
                # a temporary hardlink deleted at return does not.
                payload = str(item.path)
            else:
                data = item.path.read_bytes() if isinstance(item, FileRecord) else item.data
                payload = {"name": metadata.filename, "mimeType": metadata.mime_type, "buffer": data}
            await self._open_upload_menu(page)
            selector = 'input[type="file"][accept^="image"]' if image else 'input[type="file"]'
            await page.locator(selector).first.set_input_files(payload)
            images += int(image)
            deadline = asyncio.get_running_loop().time() + self.settings.browser_timeout_seconds
            while True:
                state = await page.evaluate(READ_COMPOSER_ATTACHMENTS)
                expected_name = Path(metadata.filename).stem
                present = state["image_count"] >= baseline["image_count"] + images if image else expected_name in state["text"]
                if present and not state["busy"]:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise UploadError("Gemini attachment card/readiness was not verified; message was not sent")
                await asyncio.sleep(0.15)
            verified.append({**metadata.public(), "verified": True, "verification": "composer_attachment_card"})
        return verified

    async def submit(self, mode, prompt, *, model=None, quality="auto", reasoning_effort=None,
                     allow_downgrade=False, attachments=None, on_upload_verified=None, dedicated_page=True,
                     on_submission_prepared=None):
        async with self._resources.ui_admitted():
            page = await self._managed_page()
            first = self._operation.account_index
            if first is None:
                first = gemini_account_index(getattr(page, 'url', gemini_account_home(0)))
            order = list(dict.fromkeys([first, *(await self._account_order())]))
            last_error = None
            for index in order:
                if index != first:
                    # Only a pristine, still-unsent new conversation may move.
                    await self._require_fresh_account_page(page)
                    await page.goto(gemini_account_home(index), wait_until='domcontentloaded',
                                    timeout=int(self.settings.browser_timeout_seconds * 1000))
                    await self._require_account_route(page, index)
                    self._operation.account_switch = {
                        'from_account_index': first, 'to_account_index': index,
                        'target_home_url': gemini_account_home(index),
                        'reason': getattr(last_error, 'code', None) or 'MODEL_UNAVAILABLE',
                        'requires_context_rebuild': False, 'prompt_sent_before_switch': False,
                    }
                self._operation.account_index = index
                self._operation.inputs_started = False
                self._account_attempt(index, 'checking')
                try:
                    receipt = await self._prepare_send(page, prompt, model=model, quality=quality,
                        reasoning_effort=reasoning_effort, allow_downgrade=allow_downgrade,
                        attachments=attachments or [], on_upload_verified=on_upload_verified,
                        research=mode == 'research', on_submission_prepared=on_submission_prepared)
                except Exception as exc:
                    if not self._is_account_availability_error(exc) or self._operation.inputs_started:
                        raise self._account_error(exc)
                    last_error = exc
                    self._account_attempt(index, 'unavailable', cause=getattr(exc, 'code', None) or 'MODEL_UNAVAILABLE',
                                          available=getattr(exc, 'available', None))
                    continue
                # Keep foreground ownership through the initial handshake;
                # never catch a send/confirmation error as an account fallback.
                return await self._confirm_send(page, receipt)
            assert last_error is not None
            self._operation.account_switch = None
            raise self._account_error(last_error)

    async def continue_conversation(self, conversation_url, prompt, *, mode=None, model=None,
                                    quality="auto", reasoning_effort=None, allow_downgrade=False,
                                    attachments=None, on_upload_verified=None, on_submission_prepared=None):
        async with self._resources.ui_admitted():
            try:
                page = await self._managed_page(conversation_url)
            except GeminiTaskError as exc:
                if exc.code == 'ACCOUNT_ROUTE_CHANGED':
                    await self._switch_for_client(conversation_url, exc, model=model, quality=quality,
                                                  reasoning_effort=reasoning_effort, allow_downgrade=allow_downgrade,
                                                  send_attempted=False)
                raise
            # Match the existing bridge: follow-ups discuss the report, rather
            # than silently launching another paid research run.
            self._operation.account_index = gemini_account_index(conversation_url)
            self._operation.inputs_started = False
            try:
                receipt = await self._prepare_send(page, prompt, model=model, quality=quality,
                                        reasoning_effort=reasoning_effort, allow_downgrade=allow_downgrade,
                                        attachments=attachments or [], on_upload_verified=on_upload_verified,
                                        research=False, on_submission_prepared=on_submission_prepared)
            except Exception as exc:
                if self._is_account_availability_error(exc) and not self._operation.inputs_started:
                    await self._switch_for_client(conversation_url, exc, model=model, quality=quality,
                                                  reasoning_effort=reasoning_effort, allow_downgrade=allow_downgrade,
                                                  send_attempted=False)
                raise self._account_error(exc)
            return await self._confirm_send(page, receipt)

    async def _require_account_route(self, page, index):
        await self._ready(page)
        if gemini_account_index(page.url) != index:
            raise GeminiTaskError('ACCOUNT_ROUTE_MISMATCH', 'Google opened another account; nothing was sent')

    async def _require_fresh_account_page(self, page):
        await self._draft(page)
        attachments = await page.evaluate(READ_COMPOSER_ATTACHMENTS)
        surface = await self._surface(page)
        if (surface['messages'] or surface['active'] or attachments.get('removals')
                or attachments.get('image_count') or attachments.get('busy')):
            raise GeminiTaskError('ACCOUNT_SWITCH_UNSAFE', 'The account page is no longer an empty unsent conversation; it was preserved')

    async def _switch_for_client(self, conversation_url, original_error, *, model, quality,
                                 reasoning_effort, allow_downgrade, send_attempted):
        """Select another account; the client, not the broker, rebuilds context."""
        original_index = gemini_account_index(conversation_url)
        self._operation.account_index = original_index
        self._account_attempt(original_index, 'unavailable',
                              cause=getattr(original_error, 'code', None) or 'MODEL_UNAVAILABLE',
                              available=getattr(original_error, 'available', None))
        for index in await self._account_order():
            if index == original_index:
                continue
            try:
                async with self._account_probe(index) as candidate:
                    await self._draft(candidate)
                    surface = await self._surface(candidate)
                    if self._quota_visible(surface):
                        raise GeminiTaskError('UPSTREAM_LIMIT', 'Gemini account quota is visible')
                    await self._selection(candidate, model, quality, reasoning_effort, allow_downgrade)
                    await self._prefer_account(index, page=candidate, reason='account_failover')
                    self._account_attempt(index, 'ready')
            except BrowserNeedsUserAction as exc:
                self._account_attempt(index, 'login_required', cause=getattr(exc, 'code', None))
                continue
            except Exception as exc:
                if self._is_account_availability_error(exc):
                    self._account_attempt(index, 'unavailable', cause=getattr(exc, 'code', None) or 'MODEL_UNAVAILABLE',
                                          available=getattr(exc, 'available', None))
                    continue
                raise self._account_error(exc)
            self._operation.account_switch = {
                'from_account_index': original_index, 'to_account_index': index,
                'target_home_url': gemini_account_home(index), 'original_conversation_url': conversation_url,
                'reason': getattr(original_error, 'code', None) or 'MODEL_UNAVAILABLE',
                'requires_context_rebuild': True, 'send_attempted': send_attempted,
                'client_action': 'compact_or_reassemble_context_then_submit_new_session',
                'automatic_replay': False,
            }
            submission_phase('account_context_required', **self._operation.account_switch)
            raise self._account_error(GeminiTaskError('ACCOUNT_CONTEXT_REQUIRED',
                'Gemini switched its next-request account. The client must compact or reassemble context and submit a new Session; the original question was not replayed.'))
        raise self._account_error(original_error)

    @staticmethod
    def _quota_visible(surface):
        return bool(re.search(r'usage limit|limit reached|quota|额度|达到.*上限', ' '.join(surface.get('alerts', [])), re.I))

    async def _prepare_send(self, page, prompt, *, model, quality, reasoning_effort, allow_downgrade,
                    attachments, on_upload_verified, research, on_submission_prepared=None):
        async with self._resources.ui_locked():
            await page.bring_to_front()
            await self._dismiss_discovery(page)
            await self._draft(page)
            submission_phase("page_ready")
            existing_inputs = await page.evaluate(READ_COMPOSER_ATTACHMENTS)
            if existing_inputs.get("removals") or existing_inputs.get("image_count"):
                raise BrowserNeedsUserAction("composer_attachments_present", "Gemini 输入框已有附件，服务未替换。请先处理已有附件。",
                                            user_input_kind="composer_draft", code="COMPOSER_DRAFT_PRESENT")
            surface = await self._surface(page)
            if self._quota_visible(surface):
                raise GeminiTaskError("UPSTREAM_LIMIT", "Gemini usage limit is visible; no message was sent")
            if surface["active"]:
                raise GeminiTaskError("CONVERSATION_BUSY", "Gemini is already generating; nothing was sent")
            await self._set_research(page, research)
            selection = await self._selection(page, model, quality, reasoning_effort, allow_downgrade)
            if self._operation.account_index is None:
                self._operation.account_index = gemini_account_index(page.url)
            await self._prefer_account(self._operation.account_index, page=page, reason='verified_selection')
            self._account_attempt(self._operation.account_index, 'ready')
            await self._draft(page)
            self._operation.inputs_started = True
            verified = await self._upload(page, attachments)
            if verified and on_upload_verified:
                await on_upload_verified(verified)
            await self._verify_selection(page, selection)
            composer = await self._draft(page)
            baseline = await self._surface(page)
            await composer.fill(prompt)
            await self._draft(page, prompt)
            submission_phase("draft_prepared")
            receipt = SubmitReceipt(
                conversation_url=page.url, assistant_count_before=sum(m["role"] == "assistant" for m in baseline["messages"]),
                selected_model=model, requested_quality=quality, requested_reasoning_effort=reasoning_effort,
                attachments=verified, **selection,
                user_message_ids_before=[m["id"] for m in baseline["messages"] if m["role"] == "user"],
                assistant_message_ids_before=[m["id"] for m in baseline["messages"] if m["role"] == "assistant"],
                submitted_prompt_sha256=normalized_text_sha256(prompt), submission_state="unconfirmed",
                research_expected=research,
                account_index=self._operation.account_index,
                account_attempts=list(self._operation.account_attempts),
                account_switch=self._operation.account_switch,
            )
            if on_submission_prepared:
                await on_submission_prepared(receipt)
            submission_phase("gemini_before_send", **await page.evaluate(READ_SUBMISSION_STATE))
            self._operation.acknowledgement_started = asyncio.get_running_loop().time()
            submission_phase("send_attempted")
            await page.get_by_role("button", name=re.compile(r"^(Send message|发送消息)$", re.I)).click()
        return receipt

    async def _confirm_send(self, page, receipt):
        deadline = asyncio.get_running_loop().time() + min(15, self.settings.browser_timeout_seconds)
        surface = {"messages": []}
        while asyncio.get_running_loop().time() < deadline:
            await self._check_acknowledgement_account(page, receipt)
            await self._ready(page)
            surface = await self._surface(page)
            user, replies = matched_turn(surface["messages"], receipt)
            if user:
                receipt.current_user_message_id = user["id"]
                receipt.current_assistant_message_id = replies[-1]["id"] if replies else None
            formal_url = self._formal_url(page.url)
            if formal_url:
                receipt.conversation_url = formal_url
                if user:
                    receipt.current_user_message_id = user["id"]
                    receipt.current_assistant_message_id = replies[-1]["id"] if replies else None
                    receipt.submission_state = "confirmed"
                    receipt.submission_verified_at = utc_now()
                    submission_phase("send_confirmed")
                    return receipt
            await asyncio.sleep(0.15)
        receipt.conversation_url = page.url
        self._identity_diagnostics(surface, receipt, "gemini_send_identity_pending")
        submission_phase("gemini_send_ack_pending", **await page.evaluate(READ_SUBMISSION_STATE))
        return receipt  # The manager preserves ambiguity and never resubmits.

    @staticmethod
    def _formal_url(value):
        try:
            return GEMINI.canonical_url(value)
        except (ValueError, TypeError):
            return None

    async def _check_acknowledgement_account(self, page, receipt):
        # Check the route before reading/matching content from another account.
        try:
            index = gemini_account_index(page.url)
        except (ValueError, TypeError):
            await self._ready(page)  # Preserve the real login/CAPTCHA handoff.
            raise GeminiTaskError('ACCOUNT_ROUTE_MISMATCH', 'The original pending Gemini page left its account route; no question was replayed')
        if receipt.account_index is not None and index != receipt.account_index:
            raise GeminiTaskError('ACCOUNT_ROUTE_MISMATCH', 'Gemini changed account after the send attempt; no question was replayed')

    async def _unbound_acknowledgement(self, page, receipt, surface):
        """Bound first-send acknowledgement separately from answer generation.

        With no formal URL we cannot safely reload a conversation or replay the
        click. Retain that exact page and return an honest infrastructure error.
        """
        now = asyncio.get_running_loop().time()
        if self._operation.acknowledgement_started is None:
            # A restarted broker gets one fresh observation budget, not a send.
            self._operation.acknowledgement_started = now
        budget = min(60.0, max(15.0, self.settings.browser_timeout_seconds * 2))
        if now - self._operation.acknowledgement_started >= budget:
            self._identity_diagnostics(surface, receipt, "gemini_send_ack_unavailable")
            submission_phase("gemini_send_ack_unavailable", **await page.evaluate(READ_SUBMISSION_STATE))
            raise GeminiTaskError("SEND_UNCONFIRMED",
                "Gemini did not confirm the original turn and its formal conversation URL within the acknowledgement budget after the send attempt. "
                "Delivery remains uncertain; the original page and task were retained. No question was replayed. "
                "Do not submit a duplicate or attempt a URL-based export while the Session is unbound.")
        return BrowserSnapshot('SEND_PENDING', None,
            progress='Gemini 尚未确认原发送或建立会话地址；正在有界核查原页面，不重发。',
            current_user_message_id=receipt.current_user_message_id,
            current_assistant_message_id=receipt.current_assistant_message_id,
            generation_active=bool(surface.get('active')), submission_state='unconfirmed')

    async def inspect_submission(self, receipt):
        """Observe a late first acknowledgement on this Session's owned page."""
        await self.start()
        async with self._resources.locked() as conn:
            pages = await self._pages_by_target()
            targets = set(self._operation.pages.values())
            if self._operation.session_id is not None:
                targets.update(row[0] for row in conn.execute(
                    'SELECT target_id FROM pages WHERE session_key=?', (self._operation.session_id,)))
            candidates = [(target, pages[target]) for target in targets if target in pages]
            if len(candidates) != 1:
                raise GeminiTaskError("TURN_IDENTITY_UNAVAILABLE", "The original pending Gemini page is unavailable; nothing was resent")
            target, page = candidates[0]
            owner = conn.execute('SELECT owner_pid FROM pages WHERE target_id=?', (target,)).fetchone()
            if not owner or owner[0] not in {None, os.getpid()}:
                raise GeminiTaskError("TURN_IDENTITY_UNAVAILABLE", "Pending Gemini page ownership changed; nothing was resent")
            self._operation.pages[page] = target
            conn.execute('UPDATE pages SET owner_pid=?,retained=0 WHERE target_id=?', (os.getpid(), target))
        await self._check_acknowledgement_account(page, receipt)
        await self._ready(page)
        surface, user, replies = await self._observe_turn_identity(page, receipt)
        if user:
            receipt.current_user_message_id = user['id']
            receipt.current_assistant_message_id = replies[-1]['id'] if replies else None
        if self._quota_visible(surface):
            async with self._resources.ui_locked():
                await self._switch_for_client(receipt.conversation_url,
                    GeminiTaskError('UPSTREAM_LIMIT', 'Gemini account quota reached while acknowledgement was pending'),
                    model=receipt.resolved_model or receipt.selected_model,
                    quality=receipt.requested_quality or 'auto',
                    reasoning_effort=receipt.resolved_reasoning_effort or receipt.requested_reasoning_effort,
                    allow_downgrade=False, send_attempted=True)
        if self._formal_url(page.url):
            if receipt.account_index is not None and gemini_account_index(page.url) != receipt.account_index:
                raise GeminiTaskError('ACCOUNT_ROUTE_MISMATCH', 'Gemini changed account while awaiting acknowledgement; no question was replayed')
            if user:
                receipt.conversation_url = GEMINI.canonical_url(page.url)
                receipt.current_user_message_id = user['id']
                receipt.current_assistant_message_id = replies[-1]['id'] if replies else None
                receipt.submission_state = 'confirmed'
                receipt.submission_verified_at = utc_now()
                submission_phase('send_confirmed_late')
                return await self.inspect(receipt)
            if self._same_conversation(page.url, receipt.conversation_url):
                return self._identity_pending_snapshot(page, receipt, surface)
        return await self._unbound_acknowledgement(page, receipt, surface)

    async def inspect(self, receipt):
        page = await self._observation_page(receipt.conversation_url)
        await self._ready(page)
        surface, original_user, replies = await self._observe_turn_identity(page, receipt)
        if not original_user:
            if self._quota_visible(surface):
                async with self._resources.ui_locked():
                    await self._switch_for_client(receipt.conversation_url,
                        GeminiTaskError('UPSTREAM_LIMIT', 'Gemini quota is visible while the original turn identity is unavailable'),
                        model=receipt.resolved_model or receipt.selected_model,
                        quality=receipt.requested_quality or 'auto',
                        reasoning_effort=receipt.resolved_reasoning_effort or receipt.requested_reasoning_effort,
                        allow_downgrade=False, send_attempted=True)
            return self._identity_pending_snapshot(page, receipt, surface)
        receipt.current_user_message_id = original_user["id"]
        user = original_user
        if receipt.research_expected:
            action_user, action_replies = research_turn(surface["messages"], receipt)
            if action_user:
                user, replies = action_user, action_replies
        latest_user = next((m for m in reversed(surface["messages"]) if m["role"] == "user"), None)
        is_latest = bool(latest_user and latest_user["id"] == user["id"])
        quota = " ".join(surface["alerts"])
        has_finished_reply = bool(replies and replies[-1].get('complete') and replies[-1].get('text') and not surface['active'])
        if is_latest and self._quota_visible(surface) and not has_finished_reply:
            async with self._resources.ui_locked():
                await self._switch_for_client(receipt.conversation_url,
                    GeminiTaskError('UPSTREAM_LIMIT', 'Gemini account usage limit reached'),
                    model=receipt.resolved_model or receipt.selected_model,
                    quality=receipt.requested_quality or 'auto',
                    reasoning_effort=receipt.resolved_reasoning_effort or receipt.requested_reasoning_effort,
                    allow_downgrade=False, send_attempted=True)
        if not replies:
            return BrowserSnapshot("RUNNING", page.url, progress="等待 Gemini 本轮回答。", generation_active=surface["active"])
        reply = replies[-1]
        receipt.current_assistant_message_id = reply["id"]
        if reply["plan_ready"]:
            return BrowserSnapshot("RESEARCH_PLAN_READY", page.url, progress="Gemini 研究计划已出现，准备开始研究。", generation_active=False)
        if receipt.research_expected and receipt.research_start_state != "confirmed":
            if reply["complete"] and not surface["active"] and re.search(r"[?？]", reply["text"]):
                return BrowserSnapshot("NEEDS_USER_INPUT", page.url, user_action=reply["text"],
                                       clarification_text=reply["text"], user_input_kind="content_clarification",
                                       generation_active=False)
            return BrowserSnapshot("RUNNING", page.url, progress="等待 Gemini 研究计划或明确的研究启动回执。", generation_active=surface["active"])
        active = surface["active"] if is_latest else None
        complete = bool(reply["text"] and reply["complete"] and active is not True)
        text, sources = reply["text"], reply.get("sources", [])
        if receipt.research_expected and reply.get("has_research_card"):
            report = surface.get("research_report") if is_latest else None
            complete = bool(report and report["text"] and not report["busy"] and active is False)
            if report:
                text, sources = report["text"], report.get("sources", [])
            elif is_latest and not reply.get("research_processing") and reply["id"] not in self._operation.opened_reports:
                # Open only this matched report card, never a previous report.
                self._operation.opened_reports.add(reply["id"])
                async with self._resources.ui_locked():
                    await page.locator('[id="' + reply["id"] + '"] immersive-entry-chip').click()
        # Read back the visible selection on completion; never call it backend proof.
        if complete:
            label = await self._picker(page).get_attribute("aria-label") or ""
            wanted = self._model_name(receipt.resolved_model or "")
            token = re.sub(r"^[\d.]+\s+", "", wanted or "")
            if token and token.casefold() not in label.casefold():
                raise GeminiTaskError("MODEL_CHANGED", "Gemini's visible model changed during this turn")
            expected_effort = receipt.resolved_reasoning_effort
            match = re.search(r"\b(Low|Medium|High)\b", label, re.I)
            actual_effort = match.group(1).title() if match else None
            if expected_effort and actual_effort != expected_effort:
                raise GeminiTaskError("MODEL_CHANGED", "Gemini's visible thinking level changed during this turn")
        return BrowserSnapshot("COMPLETE" if complete else "RUNNING", page.url,
                               progress=None if complete else "Gemini 正在生成本轮回答。",
                               result_text=text if complete else None,
                               sources=sources, current_user_message_id=original_user["id"],
                               current_assistant_message_id=reply["id"], submission_state=receipt.submission_state,
                               submission_verified_at=receipt.submission_verified_at, generation_active=active)

    async def _runtime_owned_page(self, receipt):
        """Find the original registered page; never create or navigate one."""
        try:
            expected = GEMINI.canonical_url(receipt.conversation_url)
        except (ValueError, TypeError):
            raise GeminiTaskError("GEMINI_RECOVERY_UNSAFE", "Runtime recovery requires the original formal Gemini URL")
        await self.start()
        async with self._resources.locked() as conn:
            pages = await self._pages_by_target()
            targets = set(self._operation.pages.values())
            if self._operation.session_id is not None:
                targets.update(row[0] for row in conn.execute(
                    'SELECT target_id FROM pages WHERE session_key=?', (self._operation.session_id,)))
            candidates = [(target, pages[target]) for target in targets if target in pages]
            if len(candidates) != 1:
                raise GeminiTaskError("TURN_NOT_VISIBLE", "The original Gemini page is unavailable or ambiguous; nothing was recreated")
            target, page = candidates[0]
            row = conn.execute('SELECT owner_pid,session_key FROM pages WHERE target_id=?', (target,)).fetchone()
            if (not row or row[0] not in {None, os.getpid()}
                    or self._operation.session_id is not None and row[1] != self._operation.session_id):
                raise GeminiTaskError("GEMINI_RECOVERY_UNSAFE", "The original Gemini page ownership changed; no page was refreshed")
            if urlparse(page.url).hostname == 'accounts.google.com':
                raise BrowserNeedsUserAction('login_required', '请在专用原生 Chrome 中完成 Google 登录后继续。', code='LOGIN_REQUIRED')
            if urlparse(page.url).hostname == 'gemini.google.com' and not await self._logged_in(page):
                await self._ready(page)
            try:
                same_url = self._same_conversation(page.url, expected)
            except (ValueError, TypeError):
                same_url = False
            if not same_url:
                raise GeminiTaskError("GEMINI_RECOVERY_ROUTE_MISMATCH", "The owned Gemini page left the original conversation; no page was refreshed")
            self._operation.pages[page] = target
            conn.execute('UPDATE pages SET owner_pid=?,retained=0 WHERE target_id=?', (os.getpid(), target))
            return page, target

    async def _runtime_recovery_surface(self, page, target, receipt):
        current_page, current_target = await self._runtime_owned_page(receipt)
        if current_target != target or current_page is not page:
            raise GeminiTaskError("GEMINI_RECOVERY_UNSAFE", "The original Gemini target changed; no page was refreshed")
        composer = await self._ready(page)
        surface = await self._surface(page)
        user, replies = matched_turn(surface['messages'], receipt)
        if user:
            # An arriving own turn makes the identity error obsolete, including
            # an answer that started generating during the durable checkpoint.
            receipt.current_user_message_id = user['id']
            if replies:
                receipt.current_assistant_message_id = replies[-1]['id']
            if receipt.submission_state != 'confirmed':
                receipt.submission_state = 'confirmed'
                receipt.submission_verified_at = utc_now()
            return surface, False
        if (await composer.inner_text()).strip():
            raise GeminiTaskError("GEMINI_RECOVERY_UNSAFE", "The Gemini composer contains a draft; no draft was discarded")
        attachments = await page.evaluate(READ_COMPOSER_ATTACHMENTS)
        if attachments.get('removals') or attachments.get('image_count') or attachments.get('busy'):
            raise GeminiTaskError("GEMINI_RECOVERY_UNSAFE", "The Gemini composer contains attachments or an upload; nothing was discarded")
        if surface.get('active') or any(m.get('research_processing') for m in surface['messages'] if m['role'] == 'assistant'):
            raise GeminiTaskError("GEMINI_RECOVERY_UNSAFE", "Gemini is generating; no active generation was refreshed")
        users = [m['id'] for m in surface['messages'] if m['role'] == 'user']
        assistants = [m['id'] for m in surface['messages'] if m['role'] == 'assistant']
        if users != receipt.user_message_ids_before or assistants != receipt.assistant_message_ids_before:
            raise GeminiTaskError("GEMINI_RECOVERY_UNSAFE", "The visible history is not the unchanged pre-send baseline; later or ambiguous turns were not refreshed")
        label = await self._picker(page).get_attribute('aria-label') or ''
        wanted = self._model_name(receipt.resolved_model or '')
        if wanted and not self._same_model(self._model_name(label), wanted):
            raise GeminiTaskError("MODEL_CHANGED", "Gemini's visible model changed; runtime recovery did not change it")
        expected_effort = receipt.resolved_reasoning_effort
        actual = re.search(r'\b(Low|Medium|High)\b', label, re.I)
        if expected_effort and (not actual or actual.group(1).title() != expected_effort):
            raise GeminiTaskError("MODEL_CHANGED", "Gemini's visible thinking level changed; runtime recovery did not change it")
        self._identity_diagnostics(surface, receipt, 'gemini_runtime_recovery_safe')
        return surface, True

    async def recover_runtime_error(self, receipt, *, attempt: int,
                                    on_recovery_prepared: Callable[[str], Awaitable[None]]) -> bool:
        """Only refresh an unchanged original page, with a durable checkpoint."""
        if type(attempt) is not int or attempt not in {1, 2}:
            raise GeminiTaskError('RUNTIME_RECOVERY_LIMIT', 'Gemini runtime recovery allows at most two prepared actions')
        if attempt in self._operation.runtime_recovery_attempts:
            raise GeminiTaskError('RUNTIME_RECOVERY_ALREADY_CONSUMED', 'This Gemini recovery action was already consumed')
        page, target = await self._runtime_owned_page(receipt)
        async with self._resources.ui_locked():
            async with asyncio.timeout(self.settings.browser_timeout_seconds):
                _, safe = await self._runtime_recovery_surface(page, target, receipt)
                if not safe:
                    return False
                await on_recovery_prepared('reload_gemini_conversation')
                self._operation.runtime_recovery_attempts.add(attempt)
                _, safe = await self._runtime_recovery_surface(page, target, receipt)
                if not safe:
                    return True  # Reserved attempt; an arriving turn made reload unnecessary.
                submission_phase('gemini_runtime_recovery_reload', attempt=attempt, target_id=target)
                try:
                    await page.reload(wait_until='domcontentloaded', timeout=max(1, int(self.settings.browser_timeout_seconds * 1000)))
                except Exception as exc:
                    raise GeminiTaskError('GEMINI_RECOVERY_RELOAD_FAILED', 'The original Gemini reload did not finish; its reserved action was not repeated') from exc
                await self._ready(page)
                await self._runtime_owned_page(receipt)
                surface, user, replies = await self._observe_turn_identity(page, receipt)
                if user:
                    receipt.current_user_message_id = user['id']
                    receipt.current_assistant_message_id = replies[-1]['id'] if replies else receipt.current_assistant_message_id
                    if receipt.submission_state != 'confirmed':
                        receipt.submission_state = 'confirmed'
                        receipt.submission_verified_at = utc_now()
                elif attempt == 2:
                    raise GeminiTaskError('TURN_NOT_VISIBLE', 'The original Gemini turn is absent after two reserved same-page reloads; delivery/persistence could not be established and no message was resent')
                return True

    async def start_research(self, receipt):
        page = await self._observation_page(receipt.conversation_url)
        surface = await self._surface(page)
        user, replies = matched_turn(surface["messages"], receipt)
        if not user or not replies or not replies[-1]["plan_ready"]:
            raise GeminiTaskError("RESEARCH_START_UNCONFIRMED", "The matched research plan is not available")
        content = page.locator('[id="' + replies[-1]["id"] + '"]')
        response = content.locator("xpath=ancestor::model-response")
        async with self._resources.ui_locked():
            await response.get_by_role("button", name=re.compile(r"^(Start research|开始研究|启动研究)$", re.I)).click()
        deadline = asyncio.get_running_loop().time() + min(15, self.settings.browser_timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            current = await self._surface(page)
            action_user, after = research_turn(current["messages"], receipt)
            if action_user:
                if self._quota_visible(current):
                    async with self._resources.ui_locked():
                        await self._switch_for_client(receipt.conversation_url,
                            GeminiTaskError('UPSTREAM_LIMIT', 'Gemini account quota reached after research start'),
                            model=receipt.resolved_model or receipt.selected_model,
                            quality=receipt.requested_quality or 'auto',
                            reasoning_effort=receipt.resolved_reasoning_effort or receipt.requested_reasoning_effort,
                            allow_downgrade=False, send_attempted=True)
                return
            await asyncio.sleep(0.2)
        raise GeminiTaskError("RESEARCH_START_UNCONFIRMED", "Research start action was not confirmed; do not repeat it")

    async def request_stop(self, receipt):
        page = await self._observation_page(receipt.conversation_url)
        surface = await self._surface(page)
        user, replies = matched_turn(surface["messages"], receipt)
        if receipt.research_expected:
            action_user, action_replies = research_turn(surface["messages"], receipt)
            if action_user:
                user, replies = action_user, action_replies
        latest = next((m for m in reversed(surface["messages"]) if m["role"] == "user"), None)
        if not user or not latest or user["id"] != latest["id"]:
            return False
        if not surface["active"]:
            return bool(replies and replies[-1]["complete"])
        stop = page.get_by_role("button", name=re.compile(r"^(Stop response|Stop research|停止回复|停止回答|停止研究)$", re.I))
        if not await stop.count():
            return False
        async with self._resources.ui_locked():
            await stop.first.click()
        deadline = asyncio.get_running_loop().time() + min(5, self.settings.browser_timeout_seconds)
        dialog_confirmed = False
        while asyncio.get_running_loop().time() < deadline:
            dialog = page.get_by_role("dialog", name=re.compile(r"Cancel research\?|取消研究|停止研究", re.I))
            if await dialog.count() and await dialog.is_visible():
                if dialog_confirmed:
                    return False
                confirm = dialog.get_by_role("button", name=re.compile(r"^Yes, cancel$|确认取消|是的.*取消", re.I))
                if not await confirm.count():
                    return False
                async with self._resources.ui_locked():
                    await confirm.click()
                dialog_confirmed = True
                await dialog.wait_for(state="hidden", timeout=2000)
            current = await self._surface(page)
            matched, _ = research_turn(current["messages"], receipt) if receipt.research_expected else matched_turn(current["messages"], receipt)
            if matched and not current["active"]:
                return True
            await asyncio.sleep(0.15)
        return False

    async def export_conversation(self, conversation_url, *, scope="all", count=10, stream_pages=False):
        page = await self._managed_page(conversation_url)
        await self._ready(page)
        surface = await self._surface(page)
        messages = [{"id": m["id"], "role": m["role"], "text": m["text"],
                     "citations": m.get("sources", [])} for m in surface["messages"]]
        report = surface.get("research_report")
        if report and report["text"] and messages and surface["messages"][-1].get("has_research_card"):
            messages[-1]["canvas"] = {"text": report["text"], "source": "visible_research_panel",
                                      "association": "unverified"}
        return {"conversation_url": page.url, "messages": messages, "attachments": [],
                "source": "gemini_rendered_text", "scope": "text_only",
                "completeness": {"messages": "unverified", "attachments": "verified", "overall": "unverified"},
                "reason": {"messages": "rendered_history_boundary_unverified", "attachments": "outside_text_only_contract"}}
