from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from html import escape
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from chatgpt_oracle.browser import (
    BrowserComposerDraftPresent,
    BrowserNeedsUserAction,
    ChatGPTBrowser,
    SubmitReceipt,
    _prompt_text_sha256,
    default_chrome_executable,
)
from chatgpt_oracle.config import Settings
from chatgpt_oracle.uploads import InMemoryUpload, UploadMetadata


URL = "https://chatgpt.com/c/oracle-boundary-fixture"
PROMPT = "【CODEX_ORACLE】\nInvestigate the provided local evidence."
QUESTION = "Please clarify which date range the local evidence covers."


def run_dom(tmp_path: Path, body: str, check, *, url: str = URL) -> None:
    """Run the production browser methods against an isolated, routed page."""
    executable = default_chrome_executable()
    if executable is None or not executable.exists():
        pytest.skip("a local Chrome or Edge executable is required")

    async def run() -> None:
        async with async_playwright() as playwright:
            chrome = await playwright.chromium.launch(headless=True, executable_path=str(executable))
            try:
                page = await chrome.new_page()
                await page.route("**/*", lambda route: route.fulfill(body=body, content_type="text/html; charset=utf-8"))
                await page.goto(url)
                browser = ChatGPTBrowser(replace(
                    Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "fixture-profile",
                    browser_timeout_seconds=2,
                ))
                browser._page = AsyncMock(return_value=page)
                browser._conversation_page = AsyncMock(return_value=page)
                await check(browser, page)
            finally:
                await chrome.close()

    asyncio.run(run())


def draft_page(*, rich: bool, text: str = "Human draft: keep my wording.") -> str:
    composer = (
        f'<div id="composer" contenteditable="true" role="textbox"><p>{escape(text)}</p></div>'
        if rich else f'<textarea id="composer" placeholder="Message ChatGPT">{escape(text)}</textarea>'
    )
    return f"""<main><div data-composer-body>{composer}
      <button aria-label="选择 ChatGPT 模型" id="picker">Medium</button>
      <button data-testid="composer-plus-btn">+</button>
      <input id="files" type="file" multiple>
      <button aria-label="Remove file human-notes.txt" id="existing-attachment">human-notes.txt</button>
      <button data-testid="send-button">Send</button>
    </div></main><script>
      window.actions={{clicks:0,uploads:0,edits:0}};
      document.addEventListener('click',()=>window.actions.clicks++);
      document.querySelector('#files').addEventListener('change',()=>window.actions.uploads++);
      document.querySelector('#composer').addEventListener('input',()=>window.actions.edits++);
    </script>"""


def incoming_attachment() -> InMemoryUpload:
    data = b"new broker evidence"
    return InMemoryUpload(UploadMetadata(
        upload_id="upl_boundary_fixture", filename="human-notes.txt", mime_type="text/plain",
        size=len(data), sha256=hashlib.sha256(data).hexdigest(), created_at=1, expires_at=60,
    ), data)


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("path", ["new", "research", "continuation", "dedicated"])
def test_existing_draft_stops_before_selection_upload_or_fill(tmp_path: Path, rich: bool, path: str) -> None:
    async def check(browser, page):
        original = await page.locator("#composer").evaluate(
            "node => ({text: 'value' in node ? node.value : node.innerText, html:node.innerHTML})"
        )
        browser._apply_selection = AsyncMock(wraps=browser._apply_selection)
        browser._upload_files = AsyncMock(wraps=browser._upload_files)
        browser._click_send = AsyncMock(wraps=browser._click_send)
        browser._activate_research = AsyncMock(wraps=browser._activate_research)
        browser._clear_uploaded_files = AsyncMock(wraps=browser._clear_uploaded_files)
        verified = AsyncMock()
        page.goto = AsyncMock(wraps=page.goto)
        page.reload = AsyncMock(wraps=page.reload)
        if path == "dedicated":
            # Exercise the resident new-chat path, whose navigation previously
            # happened before submit() could inspect the composer.
            browser.start = AsyncMock()
            browser._context = page.context
            browser._session_page = AsyncMock(return_value=page)
        with pytest.raises(BrowserNeedsUserAction) as error:
            if path == "continuation":
                await browser.continue_conversation(
                    URL, PROMPT, quality="extra_high", attachments=[incoming_attachment()], on_upload_verified=verified,
                )
            else:
                await browser.submit(
                    "research" if path == "research" else "consult", PROMPT, quality="extra_high",
                    attachments=[incoming_attachment()], on_upload_verified=verified, dedicated_page=path == "dedicated",
                )
        assert isinstance(error.value, BrowserComposerDraftPresent)
        assert error.value.code == "COMPOSER_DRAFT_PRESENT"
        assert error.value.user_input_kind == "composer_draft"
        assert "草稿" in error.value.instructions
        assert await page.locator("#composer").evaluate(
            "node => ({text: 'value' in node ? node.value : node.innerText, html:node.innerHTML})"
        ) == original
        assert await page.evaluate("window.actions") == {"clicks": 0, "uploads": 0, "edits": 0}
        assert await page.locator("#files").evaluate("node => node.files.length") == 0
        assert await page.locator("#existing-attachment").count() == 1
        for method in (
            browser._apply_selection, browser._upload_files, browser._click_send,
            browser._activate_research, browser._clear_uploaded_files, verified, page.goto, page.reload,
        ):
            method.assert_not_awaited()

    run_dom(tmp_path, draft_page(rich=rich), check, url=URL if path == "continuation" else "https://chatgpt.com/")


def test_new_submission_does_not_navigate_away_from_a_resident_draft(tmp_path: Path) -> None:
    async def check(browser, page):
        page.goto = AsyncMock(wraps=page.goto)
        with pytest.raises(BrowserComposerDraftPresent):
            await browser.submit("consult", PROMPT)
        page.goto.assert_not_awaited()
        assert await page.locator("#composer").input_value() == "Human draft: keep my wording."
        assert page.url == URL
    run_dom(tmp_path, draft_page(rich=False), check)


def test_disabled_resident_composer_draft_is_preserved_before_navigation(tmp_path: Path) -> None:
    async def check(browser, page):
        await page.locator("#composer").evaluate("node => node.disabled = true")
        page.goto = AsyncMock(wraps=page.goto)
        with pytest.raises(BrowserComposerDraftPresent):
            await browser.submit("consult", PROMPT)
        page.goto.assert_not_awaited()
        assert await page.locator("#composer").input_value() == "Human draft: keep my wording."
        assert await page.evaluate("window.actions") == {"clicks": 0, "uploads": 0, "edits": 0}
    run_dom(tmp_path, draft_page(rich=False), check)


@pytest.mark.parametrize("continuation", [False, True])
def test_draft_arriving_during_selection_is_kept_with_attachments(tmp_path: Path, continuation: bool) -> None:
    async def check(browser, page):
        async def select(_page, **_options):
            await page.locator("#composer").fill("New human draft while the picker was open")
            return {"resolved_model": None, "resolved_reasoning_effort": None,
                    "selection_source": "none", "selection_verified_at": "fixture"}
        browser._apply_selection = select
        browser._upload_files = AsyncMock(wraps=browser._upload_files)
        browser._clear_uploaded_files = AsyncMock(wraps=browser._clear_uploaded_files)
        browser._click_send = AsyncMock(wraps=browser._click_send)
        with pytest.raises(BrowserComposerDraftPresent):
            if continuation:
                await browser.continue_conversation(URL, PROMPT, attachments=[incoming_attachment()])
            else:
                await browser.submit("consult", PROMPT, attachments=[incoming_attachment()])
        assert await page.locator("#composer").input_value() == "New human draft while the picker was open"
        assert await page.locator("#existing-attachment").count() == 1
        assert await page.evaluate("window.actions") == {"clicks": 0, "uploads": 0, "edits": 1}
        browser._upload_files.assert_not_awaited()
        browser._clear_uploaded_files.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, draft_page(rich=False, text=""), check, url=URL if continuation else "https://chatgpt.com/")


@pytest.mark.parametrize("continuation", [False, True])
def test_draft_arriving_during_baseline_read_is_not_overwritten(tmp_path: Path, continuation: bool) -> None:
    async def check(browser, page):
        read = browser._read_turn_surface
        async def baseline(_page):
            surface = await read(page)
            await page.locator("#composer").fill("Human draft entered during turn observation")
            return surface
        browser._read_turn_surface = baseline
        browser._click_send = AsyncMock(wraps=browser._click_send)
        with pytest.raises(BrowserComposerDraftPresent):
            if continuation:
                await browser.continue_conversation(URL, PROMPT)
            else:
                await browser.submit("consult", PROMPT)
        assert await page.locator("#composer").input_value() == "Human draft entered during turn observation"
        assert await page.evaluate("window.actions") == {"clicks": 0, "uploads": 0, "edits": 1}
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, draft_page(rich=False, text=""), check, url=URL if continuation else "https://chatgpt.com/")


def turn_body(answer: str | None, *, extra: str = "", old_answer: str = "Old answer", active: bool = False) -> str:
    current = (
        f'<div style="white-space:pre-wrap" data-message-author-role="assistant" data-message-id="assistant-current" aria-busy="{str(active).lower()}">{escape(answer)}</div>'
        if answer is not None else ""
    )
    return f"""<main>
      <div data-message-author-role="user" data-message-id="user-old">Earlier question</div>
      <div data-message-author-role="assistant" data-message-id="assistant-old">{escape(old_answer)}</div>
      <div data-message-author-role="user" data-message-id="user-current">{escape(PROMPT)}</div>
      {current}{extra}
    </main>"""


def submission(*, confirmed: bool = False) -> SubmitReceipt:
    return SubmitReceipt(
        URL, 1, user_message_ids_before=["user-old"], assistant_message_ids_before=["assistant-old"],
        submitted_prompt_sha256=_prompt_text_sha256(PROMPT), submission_state="confirmed" if confirmed else "unconfirmed",
        current_user_message_id="user-current" if confirmed else None,
    )


@pytest.mark.parametrize("question", [
    QUESTION,
    "Before I proceed, could you confirm the local sample size?",
    "What are your goals and preferred deadline?",
    "请确认是否授权公开发布这份报告。",
    "你更看重速度还是准确性？",
    "Please clarify which CAPTCHA failure the local evidence demonstrates.",
])
def test_current_clarification_is_readable_for_main_without_completion(tmp_path: Path, question: str) -> None:
    async def check(browser, page):
        receipt = submission()
        snapshot = await browser.inspect(receipt)
        assert snapshot.state == "NEEDS_USER_INPUT"
        assert snapshot.user_input_kind == "content_clarification"
        assert snapshot.clarification_text == question
        assert snapshot.result_text is None
        assert snapshot.current_user_message_id == "user-current"
        assert snapshot.current_assistant_message_id == "assistant-current"
        assert snapshot.submission_state == "confirmed"
        assert snapshot.generation_active is False
    run_dom(tmp_path, turn_body(question), check)


@pytest.mark.parametrize("active_surface", ["stop", "assistant"])
def test_generating_clarification_is_not_exposed(tmp_path: Path, active_surface: str) -> None:
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "RUNNING"
        assert snapshot.generation_active is True
        assert snapshot.clarification_text is None
        assert snapshot.user_input_kind is None
        assert snapshot.result_text is None
    run_dom(tmp_path, turn_body(
        QUESTION, active=active_surface == "assistant",
        extra='<button data-testid="stop-button" aria-label="Stop generating">Stop</button>' if active_surface == "stop" else "",
    ), check)


def test_old_clarification_is_not_exposed_as_current(tmp_path: Path) -> None:
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "RUNNING"
        assert snapshot.clarification_text is None
        assert snapshot.user_input_kind is None
        assert snapshot.result_text is None
    run_dom(tmp_path, turn_body(None, old_answer=QUESTION), check)


def test_interleaved_human_turn_keeps_its_clarification_out_of_broker_turn(tmp_path: Path) -> None:
    extra = f"""<div data-message-author-role="user" data-message-id="user-human">A later human question</div>
      <div data-message-author-role="assistant" data-message-id="assistant-human">{escape(QUESTION)}</div>"""
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "COMPLETE"
        assert snapshot.result_text == "Current finished answer"
        assert snapshot.current_assistant_message_id == "assistant-current"
        assert snapshot.clarification_text is None
        assert snapshot.user_input_kind is None
    run_dom(tmp_path, turn_body("Current finished answer", extra=extra), check)


def test_unconfirmed_output_does_not_expose_a_question(tmp_path: Path) -> None:
    body = f'<main><div data-message-author-role="assistant" data-message-id="assistant-unrelated">{escape(QUESTION)}</div></main>'
    async def check(browser, page):
        snapshot = await browser.inspect(submission())
        assert snapshot.state == "NEEDS_USER_INPUT"
        assert snapshot.user_input_kind == "manual_browser"
        assert snapshot.clarification_text is None
        assert snapshot.submission_state == "unconfirmed"
    run_dom(tmp_path, body, check)


@pytest.mark.parametrize("blocker", [
    '<button>Log in</button>',
    '<p>Verify you are human</p>',
    '<iframe title="CAPTCHA" src="about:blank"></iframe>',
    '<div role="dialog">Account agreement requires review</div>',
])
def test_browser_blockers_remain_manual_and_hide_content_clarification(tmp_path: Path, blocker: str) -> None:
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "NEEDS_USER_INPUT"
        assert snapshot.user_input_kind == "manual_browser"
        assert snapshot.clarification_text is None
        assert snapshot.result_text is None
        assert snapshot.user_action
    run_dom(tmp_path, turn_body(QUESTION, extra=blocker), check)


def test_explicit_login_route_is_manual_even_with_a_rendered_current_turn(tmp_path: Path) -> None:
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "NEEDS_USER_INPUT"
        assert snapshot.user_input_kind == "manual_browser"
        assert snapshot.clarification_text is None
        assert snapshot.result_text is None
    run_dom(tmp_path, turn_body(QUESTION), check, url="https://chatgpt.com/auth/login")


@pytest.mark.parametrize("answer", [
    "An onboarding prompt may include the phrase please clarify, but the evidence supports the conclusion above.",
    "Example wording:\n> Please clarify the question.\nThis is the final analysis.",
    "Example code:\n```text\nPlease clarify the question.\n```\nThe evidence is conclusive.",
    "Verify you are human",
])
def test_assistant_mentions_and_quoted_examples_are_not_manual_or_clarification(tmp_path: Path, answer: str) -> None:
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "COMPLETE"
        assert snapshot.result_text == answer
        assert snapshot.clarification_text is None
        assert snapshot.user_input_kind is None
    run_dom(tmp_path, turn_body(answer), check)


def test_earlier_assistant_fragment_does_not_make_final_answer_a_clarification(tmp_path: Path) -> None:
    extra = '<div data-message-author-role="assistant" data-message-id="assistant-final">Evidence resolved the question.</div>'
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "COMPLETE"
        assert snapshot.current_assistant_message_id == "assistant-final"
        assert snapshot.clarification_text is None
        assert snapshot.user_input_kind is None
    run_dom(tmp_path, turn_body(QUESTION, extra=extra), check)


@pytest.mark.parametrize("quoted", ["blockquote", "pre"])
def test_rendered_quote_or_code_is_not_an_addressed_question(tmp_path: Path, quoted: str) -> None:
    body = turn_body(None, extra=f"""<div data-message-author-role="assistant" data-message-id="assistant-current">
      <p>Example wording:</p><{quoted}>{escape(QUESTION)}</{quoted}><p>The evidence is conclusive.</p>
    </div>""")
    async def check(browser, page):
        snapshot = await browser.inspect(submission(confirmed=True))
        assert snapshot.state == "COMPLETE"
        assert QUESTION in snapshot.result_text
        assert snapshot.clarification_text is None
        assert snapshot.user_input_kind is None
    run_dom(tmp_path, body, check)
