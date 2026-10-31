from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

from chatgpt_oracle.browser import (
    BrowserComposerDraftPresent, BrowserPreparationError, BrowserTaskError,
    _prompt_text_sha256,
)
from chatgpt_oracle.submission_diagnostics import submission_diagnostics, submission_phase
from test_oracle_browser_boundaries import run_dom


URL = "https://chatgpt.com/c/draft-research-fixture"
PROMPT = "核对已有证据的风险。\n\n代码与空格必须保留：\n```python\nif available:\n    result = 'a  b'\n```"
# Tool-only structure captured in oracle-20261028-readonly-page.json, without
# conversation text. Nested icons/noneditable wrappers are native tool content.
NATIVE_RESEARCH_MENTION = '''<span app-mention-name="app" app-mention-display-name="深度研究" app-mention-path="app://connector_openai_deep_research" app-mention-icon="/images/ecosystem/apps/deep_research_app/icon.png" app-mention-brand-color="" data-prompt-link-href="app://connector_openai_deep_research" data-prompt-link-label="$app" class="Mention-hIgCkC" data-appearance="inline-mention" data-layout="inline-flow" data-font-weight="medium" data-tone="accent" data-interactive="" data-underline-on-hover="" contenteditable="false"><span class="IconContainer-K62H88 SlotSizedIconContainer-xXGVqL" contenteditable="false"><span class="block overflow-hidden rounded-2xs rounded-2xs Icon-TZxsTm SlotSizedIcon-mWPuaA"><img alt="深度研究" class="h-full w-full object-cover" draggable="false" src="https://chatgpt.com/images/ecosystem/apps/deep_research_app/icon.png"></span></span><span class="Label-arpLwJ">深度研究</span></span>'''


def draft_fixture(*, insertion: str = "selection", initial: str = "") -> str:
    return """<main><div data-composer-body>
      <div contenteditable="true" role="textbox" id="composer" style="white-space:pre-wrap">INITIAL</div>
      <button data-testid="composer-plus-btn" id="plus">+</button>
      <button data-testid="send-button" id="send">Send</button>
      <div data-mention-section-id="chatgpt-actions" id="apps" hidden>
        <button id="research">Deep research</button>
      </div>
    </div></main><script>
      const composer=document.querySelector('#composer');
      window.sent=0; window.fills=0;
      document.querySelector('#plus').onclick=()=>document.querySelector('#apps').hidden=false;
      composer.addEventListener('input',()=>{
        window.fills++;
        if (composer.querySelector('[app-mention-path]') || !composer.firstChild) return;
        // Reproduce fill preserving a stale caret in the middle of Chinese.
        const range=document.createRange(), selection=window.getSelection();
        range.setStart(composer.firstChild, Math.min(7, composer.firstChild.length));
        range.collapse(true); selection.removeAllRanges();selection.addRange(range);
      });
      document.querySelector('#research').onclick=()=>{
        const template=document.createElement('template');template.innerHTML=NATIVE_MENTION;
        const mention=template.content.firstElementChild;
        if ('INSERTION'==='selection') {
          const selection=window.getSelection(), range=selection.getRangeAt(0);
          const fragment=document.createDocumentFragment();fragment.append(mention,document.createTextNode(' '));
          range.insertNode(fragment);
        } else {
          const text=composer.innerText, offset=text.indexOf('的风险')+1;
          composer.replaceChildren(document.createTextNode(text.slice(0,offset)),mention,
            document.createTextNode(('INSERTION'==='double'?'  ':' ')+text.slice(offset)));
        }
        document.querySelector('#apps').hidden=true;
      };
      document.querySelector('#send').onclick=()=>window.sent++;
    </script>""".replace("NATIVE_MENTION", json.dumps(NATIVE_RESEARCH_MENTION, ensure_ascii=False)).replace("INSERTION", insertion).replace("INITIAL", initial)


def prepare_browser(browser, page):
    browser._prepare_submission_page = AsyncMock(return_value=page)
    browser._apply_selection = AsyncMock(return_value={
        "resolved_model": "GPT fixture", "resolved_reasoning_effort": "Extra High",
        "selection_source": "fixture", "selection_verified_at": "fixture",
    })
    browser._upload_files = AsyncMock(return_value=[])
    browser._verify_selection_after_upload = AsyncMock()
    browser._cleanup_failed_uploads = AsyncMock()
    browser._click_send = AsyncMock(wraps=browser._click_send)


async def prepare(browser, page, *, continuation=False, **kwargs):
    if continuation:
        return await browser._prepare_continuation(page.url, PROMPT, mode="research", quality="extra_high", **kwargs)
    return await browser._prepare_submit("research", PROMPT, quality="extra_high", **kwargs)


def diagnostics(path: Path, task: str = "draft-fixture") -> dict:
    return json.loads((path / "diagnostics" / f"{task}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("insertion", ["selection", "middle"])
def test_research_body_and_exact_turn_hash_survive_tool_activation(tmp_path, continuation, insertion):
    async def check(browser, page):
        prepare_browser(browser, page)
        checkpoint = AsyncMock()
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            _, receipt = await prepare(browser, page, continuation=continuation, on_submission_prepared=checkpoint)
        composer = page.locator("#composer")
        state = await browser._read_composer_draft(composer)
        assert _prompt_text_sha256(state["prompt_text"]) == _prompt_text_sha256(PROMPT)
        assert "    result = 'a  b'" in state["prompt_text"]
        assert "的 风险" not in state["prompt_text"]
        assert state["research_mentions"] == 1
        assert receipt.submitted_prompt_sha256 == _prompt_text_sha256(PROMPT)
        assert receipt.research_expected is True
        assert receipt.resolved_reasoning_effort == "Extra High"
        assert await page.evaluate("window.sent") == 1
        browser._click_send.assert_awaited_once_with(page)
        browser._apply_selection.assert_awaited_once_with(
            page, quality="extra_high", model=None, reasoning_effort=None, allow_downgrade=False)
        browser._cleanup_failed_uploads.assert_not_awaited()
        checkpoint.assert_awaited_once_with(receipt)
        evidence = diagnostics(tmp_path)
        phases = [event["phase"] for event in evidence["events"]]
        assert phases.index("preparation_started") < phases.index("draft_claimed") < phases.index("draft_filled")
        assert phases.index("draft_filled") < phases.index("research_tool_activating") < phases.index("draft_prepared")
        assert ("research_separator_repaired" in phases) is (insertion == "middle")
        assert evidence["milestones"]["preparation_started"]["request_text_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
        assert evidence["milestones"]["research_tool_prepared"]["rendered_text_sha256"]
        assert PROMPT not in json.dumps(evidence, ensure_ascii=False)
    run_dom(tmp_path, draft_fixture(insertion=insertion), check, url=URL if continuation else "https://chatgpt.com/")


@pytest.mark.parametrize("initial", [
    "Human draft: keep it.",
    "核对已有证据的<span app-mention-path='app://connector_openai_deep_research'>Deep research</span> 风险。",
    NATIVE_RESEARCH_MENTION,
    '<img alt="human image" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">',
    '<span contenteditable="false" data-attachment-id="human-attachment"><svg></svg></span>',
])
def test_unknown_old_or_inherited_research_draft_is_never_adopted(tmp_path, initial):
    async def check(browser, page):
        prepare_browser(browser, page)
        original = await page.locator("#composer").inner_html()
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(BrowserComposerDraftPresent):
                await prepare(browser, page)
        assert await page.locator("#composer").inner_html() == original
        assert await page.evaluate("window.fills") == 0
        browser._apply_selection.assert_not_awaited()
        browser._upload_files.assert_not_awaited()
        browser._click_send.assert_not_awaited()
        assert diagnostics(tmp_path)["milestones"]["preparation_started"]["preparation_id"]
    run_dom(tmp_path, draft_fixture(initial=initial), check, url="https://chatgpt.com/")


@pytest.mark.parametrize("after_tool", [False, True])
def test_activation_failure_cleans_only_this_unsent_preparation(tmp_path, after_tool):
    async def check(browser, page):
        prepare_browser(browser, page)
        activate = browser._activate_research
        async def fail(current):
            if after_tool:
                await activate(current)
            raise BrowserTaskError("fixture tool failure")
        browser._activate_research = fail
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(BrowserTaskError, match="fixture tool failure"):
                await prepare(browser, page)
        state = await browser._read_composer_draft(page.locator("#composer"))
        assert not state["text"].strip()
        assert not state["has_mention"]
        assert diagnostics(tmp_path)["milestones"]["draft_cleanup_finished"]["cleared"] is True
        browser._cleanup_failed_uploads.assert_not_awaited()
        browser._click_send.assert_not_awaited()
        # A subsequent task sees empty input; no old draft adoption or resend.
        await browser._check_composer_draft(page)
    run_dom(tmp_path, draft_fixture(insertion="middle"), check, url="https://chatgpt.com/")


def test_cancellation_before_send_cleans_proven_owned_body(tmp_path):
    async def check(browser, page):
        prepare_browser(browser, page)
        browser._activate_research = AsyncMock(side_effect=asyncio.CancelledError())
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(asyncio.CancelledError):
                await prepare(browser, page)
        assert not (await page.locator("#composer").inner_text()).strip()
        browser._cleanup_failed_uploads.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


def test_tool_menu_click_without_activation_never_sends(tmp_path):
    async def check(browser, page):
        prepare_browser(browser, page)
        browser.settings = replace(browser.settings, browser_timeout_seconds=1)
        await page.locator("#research").evaluate("node => node.onclick = () => document.querySelector('#apps').hidden = true")
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(BrowserPreparationError, match="未观察到"):
                await prepare(browser, page)
        assert not (await page.locator("#composer").inner_text()).strip()
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


def test_selected_research_control_is_retained_without_toggling_it_off(tmp_path):
    async def check(browser, page):
        prepare_browser(browser, page)
        await page.evaluate("""() => {
            const selected = document.createElement('button');selected.id='selected-research';
            selected.textContent='Deep research';selected.setAttribute('aria-pressed','true');
            selected.onclick=()=>selected.setAttribute('aria-pressed','false');
            document.querySelector('main').append(selected);
        }""")
        _, receipt = await prepare(browser, page)
        assert receipt.research_expected is True
        assert await page.locator("#selected-research").get_attribute("aria-pressed") == "true"
        assert not await page.locator("#apps").is_visible()
        assert await page.evaluate("window.sent") == 1
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


# Captured from the live composer: the app decorates an inline-code hex color
# with its own aria-hidden, empty, non-editable swatch widget. The widget holds
# no user content, so it must not be reported as foreign embedded material.
HEX_SWATCH = ('<span aria-hidden="true" class="ms-1 me-1 inline-block size-3 rounded-[3px] border '
              'border-default align-[-0.08em] ProseMirror-widget" contenteditable="false" '
              'data-hex-color-swatch="#E2E8F0" style="background-color: rgb(226, 232, 240);"></span>')


@pytest.mark.parametrize("suffix,expected", [
    (HEX_SWATCH, False),
    ('<img alt="human image" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">', True),
    ('<img aria-hidden="true" alt="" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">', True),
    ('<svg aria-hidden="true"></svg>', True),
    ('<span contenteditable="false" aria-hidden="true">visible user note</span>', True),
    ('<span contenteditable="false" aria-hidden="true">' + HEX_SWATCH + '</span>', True),
    ('<span contenteditable="false">' + HEX_SWATCH + '</span>', True),
])
def test_only_contentless_hidden_widgets_are_not_user_material(tmp_path, suffix, expected):
    """A bare decoration is ignored; anything carrying content still counts."""
    async def check(browser, page):
        await page.locator("#composer").evaluate(
            "(node, html) => { node.insertAdjacentHTML('beforeend', html); }", suffix)
        state = await browser._read_composer_draft(page.locator("#composer"))
        assert state["has_embedded_content"] is expected
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


@pytest.mark.parametrize("continuation", [False, True])
def test_inline_code_hex_swatch_does_not_block_an_owned_submission(tmp_path, continuation):
    """Regression: an app-added color swatch must not revoke our own draft."""
    async def check(browser, page):
        prepare_browser(browser, page)
        fill = browser._fill_prepared_draft
        decorated = []

        async def fill_then_decorate(current_page, preparation):
            await fill(current_page, preparation)
            if not decorated:
                # The app decorates the inline code right after our own fill.
                await current_page.locator("#composer").evaluate(
                    "(node, html) => { node.insertAdjacentHTML('beforeend', html); }", HEX_SWATCH)
                decorated.append(True)

        browser._fill_prepared_draft = fill_then_decorate
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            _, receipt = await prepare(browser, page, continuation=continuation)
        assert receipt.research_expected is True
        assert await page.evaluate("window.sent") == 1
        phases = [event["phase"] for event in diagnostics(tmp_path)["events"]]
        assert "draft_validation_failed" not in phases
        assert "draft_prepared" in phases
    run_dom(tmp_path, draft_fixture(), check, url=URL if continuation else "https://chatgpt.com/")


@pytest.mark.parametrize("edit", ["same", "changed"])
def test_human_editor_interaction_revokes_cleanup_even_for_same_text(tmp_path, edit):
    async def check(browser, page):
        prepare_browser(browser, page)
        async def verification_failure(current, _selection):
            composer = current.locator("#composer")
            if edit == "changed":
                await composer.press("End")
                await composer.press_sequentially(" Human addition")
            else:
                await composer.press("ArrowLeft")
            raise BrowserTaskError("fixture final selection failure")
        browser._verify_selection_after_upload = verification_failure
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(BrowserTaskError, match="final selection failure"):
                await prepare(browser, page)
        state = await browser._read_composer_draft(page.locator("#composer"))
        assert state["interrupted"] is True
        assert state["has_mention"] is True
        assert bool(state["text"].strip())
        if edit == "changed":
            assert "Human addition" in state["text"]
        browser._cleanup_failed_uploads.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


def test_unexplained_tool_mutation_is_preserved_without_fuzzy_comparison(tmp_path):
    async def check(browser, page):
        prepare_browser(browser, page)
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(BrowserPreparationError, match="未保持本任务正文"):
                await prepare(browser, page)
        state = await browser._read_composer_draft(page.locator("#composer"))
        assert "的  风险" in state["prompt_text"]
        browser._cleanup_failed_uploads.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, draft_fixture(insertion="double"), check, url="https://chatgpt.com/")


def test_matching_separator_without_current_preparation_is_never_repaired(tmp_path):
    initial = "核对已有证据的<span app-mention-path='app://connector_openai_deep_research'>Deep research</span> 风险。"
    async def check(browser, page):
        original = await page.locator("#composer").inner_html()
        with pytest.raises(BrowserComposerDraftPresent):
            await browser._check_composer_draft(page, expected_text="核对已有证据的风险。")
        assert await page.locator("#composer").inner_html() == original
    run_dom(tmp_path, draft_fixture(initial=initial), check, url="https://chatgpt.com/")


def test_human_draft_arriving_between_claim_and_fill_is_not_overwritten(tmp_path):
    async def check(browser, page):
        prepare_browser(browser, page)
        read = browser._read_composer_draft
        async def raced_fill(composer, **options):
            if options.get("action") == "fill":
                await composer.press_sequentially("Human")
            return await read(composer, **options)
        browser._read_composer_draft = raced_fill
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(BrowserComposerDraftPresent):
                await prepare(browser, page)
        assert await page.locator("#composer").inner_text() == "Human"
        browser._click_send.assert_not_awaited()
        browser._cleanup_failed_uploads.assert_not_awaited()
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


def test_uncertain_send_is_not_cleaned_or_prepared_again(tmp_path):
    async def check(browser, page):
        prepare_browser(browser, page)
        browser._click_send = AsyncMock(side_effect=PlaywrightError("fixture dispatch timeout"))
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            _, receipt = await prepare(browser, page)
        assert receipt.submission_state == "unconfirmed"
        browser._click_send.assert_awaited_once()
        original = await page.locator("#composer").inner_html()
        browser._scope.send_attempted = False  # process/operation restart still has durable evidence
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with pytest.raises(BrowserPreparationError, match="已记录发送尝试"):
                await prepare(browser, page)
        assert diagnostics(tmp_path)["send_attempted"] is True
        assert await page.locator("#composer").inner_html() == original
        browser._click_send.assert_awaited_once()
        browser._cleanup_failed_uploads.assert_not_awaited()
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


def test_missing_preparation_checkpoint_prevents_first_edit(tmp_path):
    async def check(browser, page):
        prepare_browser(browser, page)
        with submission_diagnostics(tmp_path, "draft-fixture", "chatgpt"):
            with patch("chatgpt_oracle.submission_diagnostics.os.replace", side_effect=OSError("fixture disk failure")):
                with pytest.raises(BrowserPreparationError, match="无法持久化"):
                    await prepare(browser, page)
        assert await page.evaluate("window.fills") == 0
        assert not await page.locator("#composer").inner_text()
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, draft_fixture(), check, url="https://chatgpt.com/")


def test_prior_send_milestone_survives_new_diagnostic_scope(tmp_path):
    with submission_diagnostics(tmp_path, "same-task", "chatgpt"):
        submission_phase("draft_claimed", preparation_id="prep-fixture", send_attempted=False)
        submission_phase("send_attempted", preparation_id="prep-fixture")
    evidence = diagnostics(tmp_path, "same-task")
    assert evidence["send_attempted"] is True
    assert evidence["preparation"]["send_attempted"] is True
    assert evidence["milestones"]["send_attempted"]["phase"] == "send_attempted"
