from __future__ import annotations

from html import escape
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import _prompt_text_sha256
from test_browser_verification_alerts import PROMPT, body, receipt
from test_oracle_browser_boundaries import URL, run_dom
from test_browser_runtime_recovery import protect_submit

FAILURE_CARD = '<main><div>无法加载此 ChatGPT 对话</div><button id="again">再试一次</button></main>'
# The known failing card is not a conversation, so the fixture keeps the
# composer-less card and only checks surface/snapshot classification.
PROSE_CARD = (
    '<main><div data-message-author-role="user" data-message-id="current-user">'
    f'{escape(PROMPT)}</div>'
    '<div data-message-author-role="assistant" data-message-id="a1">'
    '“无法加载此 ChatGPT 对话”是页面级错误，不是回答内容。</div></main>'
)


def test_failure_card_is_a_recoverable_runtime_error_not_an_answer(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        surface = await browser._read_turn_surface(page)
        assert surface["verification_required"] is False
        assert surface["runtime_error_code"] == "site_conversation_load_failed"
        snapshot = await browser.inspect(receipt())
        assert snapshot.state == "RECOVERABLE_ERROR"
        assert snapshot.runtime_error_code == "site_conversation_load_failed"
        assert snapshot.user_input_kind is None and snapshot.user_action is None
        assert snapshot.result_text is None
        # Attempt 1 reaches for the card's own retry control.
        checkpoint = AsyncMock()
        current = receipt()
        current.assistant_message_ids_before = ["previous-answer"]
        assert await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint) is True
        checkpoint.assert_awaited_once_with("retry_site_alert")
        browser._click_send.assert_not_awaited()
        browser._new_page.assert_not_awaited()
    run_dom(tmp_path, FAILURE_CARD, check)


def test_conversation_prose_that_mentions_the_error_card_is_not_classified(tmp_path):
    async def check(browser, page):
        surface = await browser._read_turn_surface(page)
        assert surface["runtime_error_code"] is None
    run_dom(tmp_path, PROSE_CARD, check)


def test_committed_turn_on_home_route_is_a_recoverable_error_that_reopens_the_route(tmp_path):
    async def check(browser, page):
        protect_submit(browser)
        goto = AsyncMock()
        page.goto = goto
        current = receipt()
        snapshot = await browser.inspect(current)
        assert snapshot.state == "RECOVERABLE_ERROR"
        assert snapshot.runtime_error_code == "conversation_route_left"
        assert snapshot.result_text is None
        checkpoint = AsyncMock()
        assert await browser.recover_runtime_error(current, attempt=1, on_recovery_prepared=checkpoint) is True
        checkpoint.assert_awaited_once_with("open_conversation_route")
        goto.assert_awaited_once()
        assert goto.await_args.args[0] == URL
        browser._click_send.assert_not_awaited()
    run_dom(tmp_path, '<main><textarea></textarea></main>', check, url="https://chatgpt.com/")


@pytest.mark.parametrize("route", ["https://chatgpt.com/c/local-chatgpt%3Apending", "https://chatgpt.com/"])
def test_status_route_without_committed_conversation_cannot_bind_or_recover(tmp_path, route):
    async def check(browser, page):
        current = receipt()
        current.conversation_url = route
        assert await browser._read_turn_surface(page) is not None
        assert browser._runtime_route_matches(route, route) is False
    run_dom(tmp_path, '<main><textarea></textarea></main>', check, url=route)


def _provisional_body():
    return (
        f'<main><div data-map-composer-conversation="local-chatgpt:abc" '
        f'data-message-author-role="user" data-message-id="fallback-turn-0:0">{escape(PROMPT)}</div>'
        '<div data-talvt-turn-state="complete"></div><textarea></textarea></main>'
    )


def test_uncommitted_provisional_turn_times_out_early_into_recovery(tmp_path):
    async def check(browser, page):
        current = receipt()
        current.current_user_message_id = "fallback-turn-0:0"
        current.submitted_prompt_sha256 = _prompt_text_sha256(PROMPT)
        first = await browser.inspect(current)
        assert first.state == "RUNNING"
        assert current.provisional_uncommitted_since
        # Simulate that the bounded wait has elapsed without a commit.
        current.provisional_uncommitted_since = "2000-01-01T00:00:00+00:00"
        second = await browser.inspect(current)
        assert second.state == "RECOVERABLE_ERROR"
        assert second.runtime_error_code == "provisional_uncommitted"
        assert second.result_text is None
    run_dom(tmp_path, _provisional_body(), check, url="https://chatgpt.com/c/local-chatgpt%3Aabc")


def test_provisional_timer_clears_once_a_conversation_is_committed(tmp_path):
    async def check(browser, page):
        current = receipt()
        current.current_user_message_id = "fallback-turn-0:0"
        current.submitted_prompt_sha256 = _prompt_text_sha256(PROMPT)
        await page.goto("https://chatgpt.com/c/local-chatgpt%3Aabc")
        await browser.inspect(current)  # provisional route: starts the timer
        assert current.provisional_uncommitted_since
        # Arriving at the formal route with a bound turn clears the timer.
        await page.goto("https://chatgpt.com/c/oracle-boundary-fixture")
        current.conversation_url = URL
        await page.locator('[data-map-composer-conversation]').evaluate(
            "node => node.setAttribute('data-map-composer-conversation', 'server-conversation')")
        await page.locator('[data-message-author-role="user"]').evaluate(
            "node => node.setAttribute('data-message-id', 'current-user')")
        await browser.inspect(current)
        assert current.provisional_uncommitted_since is None
    run_dom(tmp_path, _provisional_body(), check, url="https://chatgpt.com/c/local-chatgpt%3Aabc")
