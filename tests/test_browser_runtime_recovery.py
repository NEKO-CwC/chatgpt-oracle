from __future__ import annotations

import asyncio
from html import escape
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from chatgpt_oracle.browser import BrowserNeedsUserAction, BrowserRuntimeRecoveryError
from test_browser_verification_alerts import PROMPT, SITE_ALERT, body, receipt
from test_oracle_browser_boundaries import URL, run_dom


def runtime_body(*, identity="current-user", extra="", answer=None):
    return body(extra=SITE_ALERT + extra, answer=answer).replace('data-message-id="current-user"', f'data-message-id="{identity}"')


def protect_submit(browser):
    browser._click_send = AsyncMock(side_effect=AssertionError("runtime recovery must not submit"))
    browser._upload_files = AsyncMock(side_effect=AssertionError("runtime recovery must not upload"))
    browser._new_page = AsyncMock(side_effect=AssertionError("runtime recovery must keep the original page"))


def test_site_retry_is_consumed_once_bound_to_original_turn_and_adds_no_user_message(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        await page.locator("#retry").evaluate('''button => button.addEventListener('click', () => {
            document.querySelector('[role=alert]').remove();
            document.querySelector('main').insertAdjacentHTML('beforeend', '<div data-message-author-role="assistant" data-message-id="recovered-answer">Recovered answer.</div>');
        })''')
        checkpoint = AsyncMock()
        current = receipt()
        assert await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint) is True
        checkpoint.assert_awaited_once_with("retry_site_alert")
        assert await page.evaluate("window.retryClicks") == 1
        assert await page.locator('[data-message-author-role="user"]').count() == 1
        assert current.current_user_message_id == "current-user"
        assert current.conversation_url == URL
        assert (await browser.inspect(current)).result_text == "Recovered answer."
        with pytest.raises(BrowserRuntimeRecoveryError) as caught:
            await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint)
        assert caught.value.code == "RUNTIME_RECOVERY_ALREADY_CONSUMED"
        assert await page.evaluate("window.retryClicks") == 1
        protect_submit_assertions(browser)
    run_dom(tmp_path, runtime_body(), check)


def protect_submit_assertions(browser):
    browser._click_send.assert_not_awaited()
    browser._upload_files.assert_not_awaited()
    browser._new_page.assert_not_awaited()


@pytest.mark.parametrize("attempt", [1, 2])
def test_reload_rebinds_fallback_identity_to_unique_latest_canonical_user_without_a_new_turn(tmp_path, attempt):
    async def check(browser, page):
        protect_submit(browser)
        current = receipt()
        current.current_user_message_id = "fallback-turn-1:0"
        requests = []
        async def canonical(route):
            requests.append(route.request.url)
            await route.fulfill(body=body(answer="Observed original answer.").replace('data-message-id="current-user"', 'data-message-id="canonical-user"'), content_type="text/html; charset=utf-8")
        await page.route("**/*", canonical)
        checkpoint = AsyncMock()
        assert await browser.recover_runtime_error(current, attempt=attempt, on_recovery_prepared=checkpoint) is True
        checkpoint.assert_awaited_once_with("reload_conversation")
        assert requests == [URL]
        assert current.current_user_message_id == "canonical-user"
        assert current.current_assistant_message_id == "current-answer"
        assert current.submission_state == "confirmed" and current.conversation_url == URL
        assert await page.locator('[data-message-author-role="user"]').count() == 1
        assert (await browser.inspect(current)).result_text == "Observed original answer."
        protect_submit_assertions(browser)
    # No safe Retry on attempt 1 chooses a read-only reload; attempt 2 always
    # reloads even when the original alert still offers a Retry button.
    markup = runtime_body(identity="fallback-turn-1:0")
    if attempt == 1:
        markup = markup.replace('<button id="retry">重试</button>', '')
    run_dom(tmp_path, markup, check)


def test_reload_does_not_rebind_an_ambiguous_repeated_prompt(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        current = receipt()
        current.current_user_message_id = "fallback-turn-1:0"
        repeated = body() + f'<div data-message-author-role="user" data-message-id="repeated-user">{escape(PROMPT)}</div>'
        await page.route("**/*", lambda route: route.fulfill(body=repeated, content_type="text/html; charset=utf-8"))
        checkpoint = AsyncMock()
        with pytest.raises(BrowserRuntimeRecoveryError) as caught:
            await browser.recover_runtime_error(current, attempt=2, on_recovery_prepared=checkpoint)
        assert caught.value.code == "RUNTIME_RECOVERY_TURN_AMBIGUOUS"
        assert current.current_user_message_id == "fallback-turn-1:0"
        checkpoint.assert_awaited_once_with("reload_conversation")
        protect_submit_assertions(browser)
    run_dom(tmp_path, runtime_body(identity="fallback-turn-1:0"), check)


@pytest.mark.parametrize("state", ["answer", "generation"])
def test_output_or_generation_arriving_before_checkpoint_performs_no_action(tmp_path, state):
    async def check(browser, page):
        protect_submit(browser)
        page.reload = AsyncMock(side_effect=AssertionError("late output must not be refreshed"))
        checkpoint = AsyncMock()
        assert await browser.recover_runtime_error(receipt(), attempt=1, on_recovery_prepared=checkpoint) is False
        checkpoint.assert_not_awaited()
        page.reload.assert_not_awaited()
        assert await page.evaluate("window.retryClicks") == 0
        protect_submit_assertions(browser)
        if state == "answer":
            # A lingering toast cannot suppress an already correlated answer.
            assert (await browser.inspect(receipt())).result_text == "Late original answer."
    extra = '<button data-testid="stop-button">Stop generating</button>' if state == "generation" else ''
    run_dom(tmp_path, runtime_body(answer="Late original answer." if state == "answer" else None, extra=extra), check)


def test_output_race_during_persistence_consumes_budget_but_does_not_click_retry(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        page.reload = AsyncMock(side_effect=AssertionError("late output must not be refreshed"))
        async def checkpoint(action):
            assert action == "retry_site_alert"
            assert await page.evaluate("window.retryClicks") == 0
            await page.evaluate('''() => {
                document.querySelector('[role=alert]').remove();
                document.querySelector('main').insertAdjacentHTML('beforeend', '<div data-message-author-role="assistant" data-message-id="late-answer">Original answer arrived.</div>');
            }''')
        checkpoint = AsyncMock(side_effect=checkpoint)
        current = receipt()
        assert await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint) is True
        checkpoint.assert_awaited_once_with("retry_site_alert")
        assert await page.evaluate("window.retryClicks") == 0
        page.reload.assert_not_awaited()
        assert (await browser.inspect(current)).result_text == "Original answer arrived."
        protect_submit_assertions(browser)
    run_dom(tmp_path, runtime_body(), check)


def test_ambiguous_retry_acknowledgement_consumes_the_click_without_repeating_it(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        control = page.locator("#retry")
        actual_click = control.click
        async def uncertain_click(**kwargs):
            await actual_click(**kwargs)
            raise PlaywrightError("dispatch happened; acknowledgement lost")
        control.click = AsyncMock(side_effect=uncertain_click)
        browser._runtime_retry_control = AsyncMock(return_value=control)
        checkpoint = AsyncMock()
        current = receipt()
        assert await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint) is True
        checkpoint.assert_awaited_once_with("retry_site_alert")
        assert await page.evaluate("window.retryClicks") == 1
        with pytest.raises(BrowserRuntimeRecoveryError) as caught:
            await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint)
        assert caught.value.code == "RUNTIME_RECOVERY_ALREADY_CONSUMED"
        assert await page.evaluate("window.retryClicks") == 1
        assert await page.locator('[data-message-author-role="user"]').count() == 1
        protect_submit_assertions(browser)
    run_dom(tmp_path, runtime_body(), check)


@pytest.mark.parametrize("failure", [OSError("cannot persist action"), asyncio.CancelledError()])
def test_failed_or_cancelled_checkpoint_never_clicks_or_reloads(tmp_path, failure):
    async def check(browser, page):
        protect_submit(browser)
        page.reload = AsyncMock(side_effect=AssertionError("unprepared reload"))
        checkpoint = AsyncMock(side_effect=failure)
        with pytest.raises(type(failure)):
            await browser.recover_runtime_error(receipt(), attempt=1, on_recovery_prepared=checkpoint)
        checkpoint.assert_awaited_once_with("retry_site_alert")
        assert not browser._scope.runtime_recovery_attempts
        assert await page.evaluate("window.retryClicks") == 0
        page.reload.assert_not_awaited()
        protect_submit_assertions(browser)
    run_dom(tmp_path, runtime_body(), check)


@pytest.mark.parametrize("unsafe,code", [
    ("draft", "RUNTIME_RECOVERY_DRAFT_PRESENT"),
    ("attachment", "RUNTIME_RECOVERY_ATTACHMENTS_PRESENT"),
    ("different_prompt", "RUNTIME_RECOVERY_TURN_NOT_VISIBLE"),
    ("later_user", "RUNTIME_RECOVERY_TURN_MISMATCH"),
    ("wrong_identity", "RUNTIME_RECOVERY_TURN_MISMATCH"),
    ("unconfirmed", "RUNTIME_RECOVERY_UNCONFIRMED"),
    ("wrong_route", "RUNTIME_RECOVERY_ROUTE_MISMATCH"),
])
def test_unsafe_runtime_recovery_is_a_coded_service_error_without_human_handoff(tmp_path, unsafe, code):
    async def check(browser, page):
        protect_submit(browser)
        page.reload = AsyncMock(side_effect=AssertionError("unsafe refresh"))
        current = receipt()
        if unsafe == "draft":
            await page.locator("textarea").fill("Keep my human draft.")
        elif unsafe == "different_prompt":
            current.submitted_prompt_sha256 = "0" * 64
        elif unsafe == "wrong_identity":
            current.current_user_message_id = "other-user"
        elif unsafe == "unconfirmed":
            current.submission_state = "unconfirmed"
        checkpoint = AsyncMock()
        with pytest.raises(BrowserRuntimeRecoveryError) as caught:
            await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint)
        assert caught.value.code == code
        checkpoint.assert_not_awaited()
        page.reload.assert_not_awaited()
        assert await page.evaluate("window.retryClicks") == 0
        if unsafe == "draft":
            assert await page.locator("textarea").input_value() == "Keep my human draft."
        protect_submit_assertions(browser)
    extra = '<button aria-label="Remove file human.txt">human.txt</button>' if unsafe == "attachment" else ''
    if unsafe == "later_user":
        extra += '<div data-message-author-role="user" data-message-id="later-user">A later question.</div>'
    run_dom(tmp_path, runtime_body(extra=extra), check, url="https://chatgpt.com/c/other-conversation" if unsafe == "wrong_route" else URL)


@pytest.mark.parametrize("url,extra,reason", [
    (URL, '<iframe title="CAPTCHA" src="about:blank"></iframe>', "verification_required"),
    ("https://chatgpt.com/auth/login", '<button>Log in</button>', "login_required"),
])
def test_actual_login_or_visible_captcha_alone_remains_manual(tmp_path, url, extra, reason):
    async def check(browser, page):
        protect_submit(browser)
        browser._scope.primary_page = page
        checkpoint = AsyncMock()
        with pytest.raises(BrowserNeedsUserAction) as caught:
            await browser.recover_runtime_error(receipt(), attempt=1, on_recovery_prepared=checkpoint)
        assert caught.value.reason == reason
        checkpoint.assert_not_awaited()
        assert await page.evaluate("window.retryClicks") == 0
        protect_submit_assertions(browser)
    run_dom(tmp_path, runtime_body(extra=extra), check, url=url)


def test_attempt_limit_prevents_any_browser_action(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        checkpoint = AsyncMock()
        for attempt in (0, 3, -1, True):
            with pytest.raises(BrowserRuntimeRecoveryError) as caught:
                await browser.recover_runtime_error(receipt(), attempt=attempt, on_recovery_prepared=checkpoint)
            assert caught.value.code == "RUNTIME_RECOVERY_LIMIT"
        checkpoint.assert_not_awaited()
        browser._conversation_page.assert_not_awaited()
        assert await page.evaluate("window.retryClicks") == 0
    run_dom(tmp_path, runtime_body(), check)


def test_presend_runtime_alert_uses_only_owned_empty_new_page_recovery(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        browser._owned_pages[page] = "local-fixture"
        browser._scope.primary_page = page
        await page.route("**/*", lambda route: route.fulfill(body='<main><textarea></textarea></main>', content_type="text/html; charset=utf-8"))
        original_reload = page.reload
        page.reload = AsyncMock(wraps=original_reload)
        prepared = await browser._prepare_submission_page(page, needs_selection=False, can_reload=True)
        assert prepared is page
        page.reload.assert_awaited_once()
        assert await page.locator('[data-message-author-role="user"]').count() == 0
        protect_submit_assertions(browser)
    run_dom(tmp_path, SITE_ALERT + '<main><textarea></textarea></main>', check, url="https://chatgpt.com/")
