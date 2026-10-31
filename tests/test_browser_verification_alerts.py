from __future__ import annotations

from html import escape
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserNeedsUserAction, BrowserPageNotReady, SubmitReceipt, _prompt_text_sha256
from test_oracle_browser_boundaries import URL, run_dom


PROMPT = "Observe the original question."
SITE_ALERT = '<aside role="alert"><div>cloudflare_challenge</div><button id="retry">重试</button></aside>'


def body(*, extra="", answer=None):
    assistant = (
        f'<div data-message-author-role="assistant" data-message-id="current-answer">{escape(answer)}</div>'
        if answer is not None else ""
    )
    return f'''<main><div data-message-author-role="user" data-message-id="current-user">{escape(PROMPT)}</div>
        {assistant}<textarea></textarea></main>{extra}
        <script>window.retryClicks=0;document.querySelector('#retry')?.addEventListener('click',()=>window.retryClicks++);</script>'''


def receipt(*, confirmed=True):
    return SubmitReceipt(
        URL, 0, submitted_prompt_sha256=_prompt_text_sha256(PROMPT),
        submission_state="confirmed" if confirmed else "unconfirmed",
        current_user_message_id="current-user" if confirmed else None,
    )


def test_visible_runtime_alert_stops_preparation_but_is_recoverable_during_observation(tmp_path):
    async def check(browser, page):
        page.reload = AsyncMock(side_effect=AssertionError("verification must remain manual"))
        browser._click_send = AsyncMock(side_effect=AssertionError("must not submit"))
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is False
        assert surface["site_alert_code"] == "cloudflare_challenge"
        assert surface["runtime_error_code"] == "cloudflare_challenge"
        assert surface["generation_active"] is False
        for action in (browser._check_verification_challenge(page), browser._require_composer(page)):
            with pytest.raises(BrowserPageNotReady):
                await action
        snapshot = await browser.inspect(receipt())
        assert snapshot.state == "RECOVERABLE_ERROR"
        assert snapshot.user_input_kind is None and snapshot.user_action is None
        assert snapshot.runtime_error_code == "cloudflare_challenge"
        assert snapshot.current_user_message_id == "current-user"
        assert snapshot.current_assistant_message_id is None
        assert snapshot.generation_active is False
        assert snapshot.result_text is None and snapshot.clarification_text is None
        assert await page.evaluate("window.retryClicks") == 0
        page.reload.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, body(extra=SITE_ALERT), check)


@pytest.mark.parametrize("url", ["https://chatgpt.com/", "https://chatgpt.com/c/local-chatgpt%3Apending", URL])
def test_pending_submission_runtime_alert_is_recoverable_on_original_page(tmp_path, url):
    async def check(browser, page):
        browser._scope.primary_page = page
        pending = receipt(confirmed=False)
        snapshot = await browser.inspect_submission(pending)
        assert snapshot.state == "RECOVERABLE_ERROR"
        assert snapshot.user_input_kind is None and snapshot.user_action is None
        assert snapshot.runtime_error_code == "cloudflare_challenge"
        assert snapshot.generation_active is False
        assert snapshot.result_text is None and snapshot.current_assistant_message_id is None
        assert snapshot.current_user_message_id == "current-user"
        assert snapshot.conversation_url == (URL if url == URL else None)
        assert pending.submission_state == ("confirmed" if url == URL else "unconfirmed")
        assert browser._scope.primary_page is page
        assert await page.evaluate("window.retryClicks") == 0
    run_dom(tmp_path, body(extra=SITE_ALERT), check, url=url)


@pytest.mark.parametrize("hidden", ["hidden", 'style="display:none"', 'style="visibility:hidden"', 'style="opacity:0"', 'aria-hidden="true"'])
def test_hidden_site_alert_does_not_block_a_finished_turn(tmp_path, hidden):
    extra = SITE_ALERT.replace('<aside role="alert">', f'<aside role="alert" {hidden}>')
    async def check(browser, page):
        await browser._check_verification_challenge(page)
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is False
        assert surface["site_alert_code"] is None
        snapshot = await browser.inspect(receipt())
        assert snapshot.state == "COMPLETE"
        assert snapshot.result_text == "Finished answer."
        assert snapshot.user_input_kind is None
        assert await page.evaluate("window.retryClicks") == 0
    run_dom(tmp_path, body(extra=extra, answer="Finished answer."), check)


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("markup", ["cloudflare_challenge", SITE_ALERT])
def test_cloudflare_prose_and_alert_markup_inside_a_turn_are_not_site_challenges(tmp_path, role, markup):
    extra = f'<div data-message-author-role="{role}" data-message-id="quoted-{role}">{markup}</div>'
    async def check(browser, page):
        await browser._check_verification_challenge(page)
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is False
        assert surface["site_alert_code"] is None
        snapshot = await browser.inspect(receipt())
        assert snapshot.state == "COMPLETE"
        assert snapshot.user_input_kind is None
        assert snapshot.result_text and "Finished answer." in snapshot.result_text
    # Place the quote in an earlier turn, so it cannot replace the current
    # answer or its user identity during the normal turn-correlation check.
    run_dom(tmp_path, extra + body(answer="Finished answer."), check)


@pytest.mark.parametrize("extra", [
    '<div>cloudflare_challenge</div>',
    '<aside role="alert"><div>A report discussing cloudflare_challenge</div></aside>',
    '<aside role="alert"><div>cloudflare_challenge_example</div></aside>',
    '<blockquote>' + SITE_ALERT + '</blockquote>',
    '<pre>' + SITE_ALERT + '</pre>',
    '<aside role="alert"><div hidden>cloudflare_challenge</div><div>Unrelated notification</div></aside>',
])
def test_only_exact_visible_site_alert_codes_are_manual_blockers(tmp_path, extra):
    async def check(browser, page):
        await browser._check_verification_challenge(page)
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is False
        assert surface["site_alert_code"] is None
        snapshot = await browser.inspect(receipt())
        assert snapshot.state == "COMPLETE"
        assert snapshot.result_text == "Finished answer."
        assert snapshot.user_input_kind is None
    run_dom(tmp_path, body(extra=extra, answer="Finished answer."), check)


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_generic_verification_phrase_quoted_in_turn_is_not_a_preparation_challenge(tmp_path, role):
    extra = f'<div data-message-author-role="{role}" data-message-id="quoted-verification"><p>Verify you are human</p></div>'
    async def check(browser, page):
        await browser._check_verification_challenge(page)
        assert (await browser._read_turn_surface(page))["verification_required"] is False
    run_dom(tmp_path, extra + body(answer="Finished answer."), check)


def test_actual_visible_captcha_takes_precedence_over_runtime_alert(tmp_path):
    async def check(browser, page):
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is True
        assert surface["runtime_error_code"] == "cloudflare_challenge"
        with pytest.raises(BrowserNeedsUserAction) as caught:
            await browser._check_verification_challenge(page)
        assert caught.value.reason == "verification_required"
        snapshot = await browser.inspect(receipt())
        assert snapshot.state == "NEEDS_USER_INPUT" and snapshot.user_input_kind == "manual_browser"
        assert snapshot.runtime_error_code is None
        browser._scope.primary_page = page
        pending = await browser.inspect_submission(receipt(confirmed=False))
        assert pending.state == "NEEDS_USER_INPUT" and pending.user_input_kind == "manual_browser"
    run_dom(tmp_path, body(extra=SITE_ALERT + '<iframe title="CAPTCHA" src="about:blank"></iframe>'), check)


def test_background_cloudflare_probe_is_runtime_but_visible_interactive_widget_is_manual(tmp_path):
    async def check(browser, page):
        await page.route("https://challenges.cloudflare.com/**", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
        await page.evaluate('''() => {
            const frame = document.createElement('iframe');
            frame.id = 'cloudflare-widget'; frame.src = 'https://challenges.cloudflare.com/widget';
            frame.style.width = '1px'; frame.style.height = '1px'; frame.style.border = '0';
            document.body.append(frame);
        }''')
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is False
        assert (await browser.inspect(receipt())).state == "RECOVERABLE_ERROR"
        await page.locator("#cloudflare-widget").evaluate("node => {node.style.width='300px';node.style.height='65px';}")
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is True
        assert (await browser.inspect(receipt())).user_input_kind == "manual_browser"
    run_dom(tmp_path, body(extra=SITE_ALERT), check)
