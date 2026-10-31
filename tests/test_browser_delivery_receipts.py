from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from chatgpt_oracle.browser import (
    BrowserComposerDraftPresent, BrowserNeedsUserAction, BrowserPageNotReady, BrowserPreparationError,
    BrowserTaskError, ChatGPTBrowser, _prompt_text_sha256, _resolve_current_turn,
)
from chatgpt_oracle.config import Settings


class FakePage:
    def __init__(self, identity="owned", url="https://chatgpt.com/", context=None):
        self.identity, self.url, self.context = identity, url, context
        self.closed = False
        self.bring_to_front = AsyncMock()
        self.reload = AsyncMock()
        self.evaluate = AsyncMock(return_value=True)
        self.goto = AsyncMock(side_effect=self.navigate)
        self.close = AsyncMock(side_effect=self.close_page)

    async def navigate(self, url, **kwargs):
        self.url = url

    async def close_page(self):
        self.closed = True
        if self.context is not None:
            self.context.pages.remove(self)

    def is_closed(self):
        return self.closed


def submission_fixture(tmp_path, *, continuation=False):
    browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile", browser_timeout_seconds=.1))
    page = FakePage(url="https://chatgpt.com/c/existing" if continuation else "https://chatgpt.com/")
    composer = type("Composer", (), {})()
    composer.text, composer.preparation_id = "", None
    async def fill(text):
        composer.text = text
    async def draft_snapshot(_script, options=None, **_kwargs):
        options = options or {}
        previous = composer.text
        if options.get("action") == "claim" and not composer.text:
            composer.preparation_id = options["preparation_id"]
        if options.get("action") == "fill" and composer.preparation_id == options.get("preparation_id"):
            await composer.fill(options["text"])
        if options.get("action") == "clear" and composer.preparation_id == options.get("preparation_id"):
            composer.text = ""
        return {"text": composer.text, "prompt_text": composer.text, "has_mention": False,
                "changed": options.get("action") == "fill" and composer.text != previous,
                "preparation_id": composer.preparation_id, "signature": "fixture:" + composer.text,
                "interrupted": False, "cleared": not composer.text}
    composer.fill = AsyncMock(side_effect=fill)
    composer.evaluate = AsyncMock(side_effect=draft_snapshot)
    browser._find_composer = AsyncMock(return_value=composer)
    browser._fresh_conversation_page = AsyncMock(return_value=page)
    browser._conversation_page = AsyncMock(return_value=page)
    browser._prepare_submission_page = AsyncMock(return_value=page)
    browser._require_composer = AsyncMock()
    browser._require_conversation_route = AsyncMock()
    browser._check_composer_draft = AsyncMock(return_value=composer)
    browser._apply_selection = AsyncMock(return_value={
        "resolved_model": "GPT fixture", "resolved_reasoning_effort": "Extra High",
        "selection_source": "explicit", "selection_verified_at": "verified",
    })
    browser._upload_files = AsyncMock(return_value=[{"filename": "evidence.zip", "verified": True}])
    browser._verify_selection_after_upload = AsyncMock()
    browser._cleanup_failed_uploads = AsyncMock()
    browser._read_turn_surface = AsyncMock(return_value={"messages": [
        {"id": "old-user", "role": "user", "text": "previous"},
        {"id": "old-answer", "role": "assistant", "text": "previous answer"},
    ]})
    browser._click_send = AsyncMock()
    browser._wait_for_conversation_url = AsyncMock(side_effect=BrowserTaskError("URL not committed"))
    browser._confirm_submission = AsyncMock()
    return browser, page, composer


@pytest.mark.parametrize("continuation", [False, True])
def test_prepared_receipt_is_persistable_before_one_click_and_survives_unknown_url(tmp_path, continuation):
    async def run():
        browser, page, composer = submission_fixture(tmp_path, continuation=continuation)
        saved = []
        async def prepared(receipt):
            browser._click_send.assert_not_awaited()
            assert not browser._scope.send_attempted
            assert composer.fill.await_args.args == ("current question",)
            assert receipt.submission_state == "unconfirmed"
            saved.append(receipt)
        kwargs = dict(model="GPT fixture", quality="extra_high", on_submission_prepared=prepared)
        if continuation:
            receipt = await browser.continue_conversation(page.url, "current question", **kwargs)
        else:
            receipt = await browser.submit("consult", "current question", dedicated_page=True, **kwargs)
        assert saved == [receipt]
        assert receipt.submission_state == "unconfirmed"
        assert receipt.conversation_url == page.url
        assert receipt.submitted_prompt_sha256 == _prompt_text_sha256("current question")
        assert receipt.user_message_ids_before == ["old-user"]
        assert receipt.assistant_message_ids_before == ["old-answer"]
        assert receipt.resolved_model == "GPT fixture"
        assert receipt.resolved_reasoning_effort == "Extra High"
        assert receipt.attachments == [{"filename": "evidence.zip", "verified": True}]
        browser._click_send.assert_awaited_once()
        browser._cleanup_failed_uploads.assert_not_awaited()
        page.reload.assert_not_awaited()
        page.close.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("continuation", [False, True])
def test_receipt_checkpoint_failure_prevents_click(tmp_path, continuation):
    async def run():
        browser, page, _ = submission_fixture(tmp_path, continuation=continuation)
        callback = AsyncMock(side_effect=OSError("cannot save receipt"))
        with pytest.raises(OSError, match="cannot save"):
            if continuation:
                await browser.continue_conversation(page.url, "current question", on_submission_prepared=callback)
            else:
                await browser.submit("consult", "current question", dedicated_page=True, on_submission_prepared=callback)
        callback.assert_awaited_once()
        browser._click_send.assert_not_awaited()
        assert not browser._scope.send_attempted
    asyncio.run(run())


def test_click_timeout_keeps_receipt_and_does_not_clean_or_send_again(tmp_path):
    async def run():
        browser, page, _ = submission_fixture(tmp_path)
        browser._click_send = AsyncMock(side_effect=PlaywrightError("click acknowledgement timed out"))
        receipt = await browser.submit("consult", "current question", dedicated_page=True)
        assert receipt.submission_state == "unconfirmed"
        assert receipt.submitted_prompt_sha256
        browser._click_send.assert_awaited_once()
        browser._cleanup_failed_uploads.assert_not_awaited()
        browser._prepare_submission_page.assert_awaited_once()
        page.reload.assert_not_awaited()
        page.close.assert_not_awaited()
    asyncio.run(run())


def test_uncommitted_url_keeps_observed_turn_ids_without_claiming_confirmed_delivery(tmp_path):
    async def run():
        browser, _, _ = submission_fixture(tmp_path)
        async def observe(page, receipt):
            _resolve_current_turn(receipt, [
                {"id": "old-answer", "role": "assistant", "text": "previous answer"},
                {"id": "new-user", "role": "user", "text": "current question"},
                {"id": "new-answer", "role": "assistant", "text": "answer"},
            ])
        browser._confirm_submission = observe
        receipt = await browser.submit("consult", "current question", dedicated_page=True)
        assert receipt.current_user_message_id == "new-user"
        assert receipt.current_assistant_message_id == "new-answer"
        assert receipt.submission_state == "unconfirmed"
        browser._click_send.assert_awaited_once()
    asyncio.run(run())


def recovery_fixture(tmp_path):
    browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile", browser_timeout_seconds=.15))
    page = FakePage()
    browser._owned_pages[page] = page.identity
    browser._scope.primary_page = page
    browser._check_composer_draft = AsyncMock()
    browser._page_requires_login = AsyncMock(return_value=False)
    browser._check_verification_challenge = AsyncMock()
    browser._page_has_composer = AsyncMock(return_value=True)
    browser._intelligence_trigger = AsyncMock()
    return browser, page


@pytest.mark.parametrize("url", ["https://chatgpt.com/", "chrome-error://chromewebdata/"])
def test_empty_owned_page_refreshes_before_upload_or_send(tmp_path, url):
    async def run():
        browser, page = recovery_fixture(tmp_path)
        page.url = url
        browser._require_composer = AsyncMock(side_effect=[BrowserPageNotReady("loading"), None])
        browser._rebuild_submission_page = AsyncMock()
        recovered = await browser._prepare_submission_page(page, needs_selection=True, can_reload=True)
        assert recovered is page
        page.reload.assert_awaited_once()
        browser._rebuild_submission_page.assert_not_awaited()
        assert not browser._scope.send_attempted
    asyncio.run(run())


def test_owned_empty_page_rebuild_rebinds_same_session_without_a_fifth_page(tmp_path):
    async def run():
        browser, original = recovery_fixture(tmp_path)
        class Context:
            def __init__(self):
                self.pages = [original] + [FakePage(f"peer-{index}", f"https://chatgpt.com/c/peer-{index}") for index in range(3)]
                self.peak = len(self.pages)
                for page in self.pages:
                    page.context = self
            async def new_page(self):
                page = FakePage("replacement", "about:blank", self)
                self.pages.append(page)
                self.peak = max(self.peak, len(self.pages))
                return page
        context = Context()
        browser._context = context
        browser.start = AsyncMock()
        browser._target_id = AsyncMock(side_effect=lambda page: page.identity)
        browser._scope.session_id = "shared-session"
        async with browser._resources.locked() as conn:
            conn.execute("INSERT INTO pages(target_id,owner_pid,session_key,session_primary) VALUES(?,?,?,1)", (original.identity, os.getpid(), "shared-session"))
        browser._require_composer = AsyncMock(side_effect=[BrowserPageNotReady("loading"), BrowserPageNotReady("still loading"), None])
        recovered = await browser._prepare_submission_page(original, needs_selection=True, can_reload=True)
        assert recovered is not original and recovered.identity == "replacement"
        original.reload.assert_awaited_once()
        original.close.assert_awaited_once()
        assert context.peak == 4 and len(context.pages) == 4
        assert browser._scope.primary_page is recovered
        assert browser._owned_pages == {recovered: "replacement"}
        async with browser._resources.locked() as conn:
            rows = list(conn.execute("SELECT target_id,session_key,session_primary FROM pages"))
        assert rows == [("replacement", "shared-session", 1)]
    asyncio.run(run())


def test_submission_after_rebuild_uses_replacement_for_upload_selection_and_single_send(tmp_path):
    async def run():
        browser, original, _ = submission_fixture(tmp_path)
        browser._owned_pages[original] = original.identity
        replacement = FakePage("replacement")
        browser._prepare_submission_page = ChatGPTBrowser._prepare_submission_page.__get__(browser)
        browser._require_composer = AsyncMock(side_effect=[BrowserPageNotReady("loading"), BrowserPageNotReady("loading"), None])
        browser._page_has_composer = AsyncMock(return_value=True)
        browser._page_requires_login = AsyncMock(return_value=False)
        browser._check_verification_challenge = AsyncMock()
        browser._intelligence_trigger = AsyncMock()
        browser._rebuild_submission_page = AsyncMock(return_value=replacement)
        receipt = await browser.submit("consult", "current question", quality="extra_high", dedicated_page=True)
        assert receipt.conversation_url == replacement.url
        browser._apply_selection.assert_awaited_once_with(replacement, quality="extra_high", model=None, reasoning_effort=None, allow_downgrade=False)
        browser._upload_files.assert_awaited_once_with(replacement, [])
        browser._click_send.assert_awaited_once_with(replacement)
        original.reload.assert_awaited_once()
        replacement.reload.assert_not_awaited()
    asyncio.run(run())


def test_a_fill_that_did_not_update_the_composer_cannot_be_sent(tmp_path):
    async def run():
        browser, _, composer = submission_fixture(tmp_path)
        browser._check_composer_draft = ChatGPTBrowser._check_composer_draft.__get__(browser)
        browser._find_composer = AsyncMock(return_value=composer)
        composer.fill.side_effect = None
        checkpoint = AsyncMock()
        with pytest.raises(BrowserPreparationError):
            await browser.submit("consult", "current question", dedicated_page=True, on_submission_prepared=checkpoint)
        composer.fill.assert_awaited_once_with("current question")
        checkpoint.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    asyncio.run(run())


def test_account_failure_after_initial_ready_check_recovers_before_input(tmp_path):
    async def run():
        browser,original,composer=submission_fixture(tmp_path)
        browser._owned_pages[original]=original.identity
        replacement=FakePage('replacement')
        browser._prepare_submission_page=AsyncMock(side_effect=[original,replacement])
        browser._can_recover_submission_page=AsyncMock(return_value=True)
        first=True
        async def check_draft(*args,**kwargs):
            nonlocal first
            if first:
                first=False
                raise BrowserPageNotReady('composer replaced after account focus')
            return composer
        browser._check_composer_draft=AsyncMock(side_effect=check_draft)
        await browser.submit('consult','one question',dedicated_page=True)
        assert browser._prepare_submission_page.await_count==2
        browser._upload_files.assert_awaited_once_with(replacement,[])
        browser._click_send.assert_awaited_once_with(replacement)
    asyncio.run(run())


@pytest.mark.parametrize("unsafe", ["foreign", "retained", "sent", "conversation", "attachment"])
def test_recovery_never_changes_unsafe_pages(tmp_path, unsafe):
    async def run():
        browser, page = recovery_fixture(tmp_path)
        if unsafe == "foreign":
            browser._owned_pages.clear()
        elif unsafe == "retained":
            browser._scope.preserved_pages.add(page)
        elif unsafe == "sent":
            browser._scope.send_attempted = True
        elif unsafe == "conversation":
            page.url = "https://chatgpt.com/c/existing"
        else:
            page.evaluate.return_value = False
        browser._require_composer = AsyncMock(side_effect=BrowserPageNotReady("loading"))
        browser._rebuild_submission_page = AsyncMock()
        with pytest.raises(BrowserPageNotReady):
            await browser._prepare_submission_page(page, needs_selection=True, can_reload=True)
        page.reload.assert_not_awaited()
        browser._rebuild_submission_page.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("intervention", ["draft", "login", "captcha"])
def test_manual_intervention_preserves_page_without_refresh(tmp_path, intervention):
    async def run():
        browser, page = recovery_fixture(tmp_path)
        browser._require_composer = AsyncMock(side_effect=BrowserPageNotReady("loading"))
        if intervention == "draft":
            browser._check_composer_draft.side_effect = BrowserComposerDraftPresent()
        elif intervention == "login":
            browser._page_requires_login.return_value = True
        else:
            browser._check_verification_challenge.side_effect = BrowserNeedsUserAction("verification_required", "manual only")
        with pytest.raises(BrowserNeedsUserAction):
            await browser._prepare_submission_page(page, needs_selection=True, can_reload=True)
        page.reload.assert_not_awaited()
        page.close.assert_not_awaited()
    asyncio.run(run())
