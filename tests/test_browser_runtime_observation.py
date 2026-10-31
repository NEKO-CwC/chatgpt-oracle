"""A recovered turn must remain observable through stale UI and ID hydration."""
from unittest.mock import AsyncMock
from dataclasses import replace

import pytest
from chatgpt_oracle.browser import BrowserRuntimeRecoveryError

from test_browser_verification_alerts import SITE_ALERT, body, receipt
from test_oracle_browser_boundaries import run_dom


@pytest.mark.parametrize('generating', [False, True])
def test_stale_runtime_alert_does_not_hide_current_output_or_generation(tmp_path, generating):
    async def check(browser, page):
        browser.recover_runtime_error = AsyncMock(side_effect=AssertionError('No recovery of active or answered turns'))
        value = await browser.inspect(receipt())
        assert value.state == ('RUNNING' if generating else 'COMPLETE')
        assert value.result_text == (None if generating else 'Original answer has arrived.')
        assert value.user_input_kind is None
        browser.recover_runtime_error.assert_not_awaited()
    extra = SITE_ALERT + ('<button data-testid="stop-button">Stop generating</button>' if generating else '')
    run_dom(tmp_path, body(extra=extra, answer=None if generating else 'Original answer has arrived.'), check)


def test_exact_original_turn_can_hydrate_from_fallback_to_canonical_after_retry(tmp_path):
    async def check(browser, page):
        current = receipt()
        current.current_user_message_id = 'fallback-turn-1:0'
        value = await browser.inspect(current)
        assert current.current_user_message_id == 'canonical-original-user'
        assert value.state == 'COMPLETE' and value.result_text == 'Recovered original answer.'
        assert value.current_assistant_message_id == 'current-answer'
        assert await page.locator('[data-message-author-role="user"]').count() == 1
    run_dom(tmp_path, body(answer='Recovered original answer.').replace('data-message-id="current-user"', 'data-message-id="canonical-original-user"'), check)


def test_repeated_prompt_does_not_rebind_an_uncertain_canonical_turn(tmp_path):
    async def check(browser, page):
        current = receipt()
        current.current_user_message_id = 'fallback-turn-1:0'
        value = await browser.inspect(current)
        assert current.current_user_message_id == 'fallback-turn-1:0'
        assert value.state != 'COMPLETE' and value.result_text is None
    repeated = body().replace('data-message-id="current-user"', 'data-message-id="other-user"')
    canonical = body(answer='Must not attribute this answer.').replace('data-message-id="current-user"', 'data-message-id="canonical-original-user"')
    run_dom(tmp_path, repeated+canonical, check)


@pytest.mark.parametrize('restored', [True, False])
def test_discarded_optimistic_turn_uses_second_reload_then_confirms_or_fails(tmp_path, restored):
    async def check(browser, page):
        current = receipt()
        current.current_user_message_id = 'fallback-turn-1:0'
        current.assistant_message_ids_before = ['previous-answer']
        current.runtime_recovery_attempts = 1
        browser.settings = replace(browser.settings, browser_timeout_seconds=10)
        snapshot = await browser.inspect(current)
        assert snapshot.state == 'RECOVERABLE_ERROR'
        assert snapshot.runtime_error_code == 'runtime_turn_missing'
        reload_body = body(answer='Restored original answer.') if restored else '<main><div data-message-author-role="assistant" data-message-id="previous-answer">Previous answer.</div><textarea></textarea></main>'
        reload_body = reload_body.replace('data-message-id="current-user"', 'data-message-id="canonical-original-user"')
        await page.route('**/*', lambda route: route.fulfill(body=reload_body, content_type='text/html; charset=utf-8'))
        checkpoint = AsyncMock()
        browser._click_send = AsyncMock(side_effect=AssertionError('A missing turn must not be resent'))
        if restored:
            assert await browser.recover_runtime_error(current, attempt=2, on_recovery_prepared=checkpoint)
            assert (await browser.inspect(current)).result_text == 'Restored original answer.'
            assert current.current_user_message_id == 'canonical-original-user'
        else:
            with pytest.raises(BrowserRuntimeRecoveryError) as caught:
                await browser.recover_runtime_error(current, attempt=2, on_recovery_prepared=checkpoint)
            assert caught.value.code == 'RUNTIME_RECOVERY_TURN_NOT_VISIBLE'
            assert current.current_user_message_id == 'fallback-turn-1:0'
        checkpoint.assert_awaited_once_with('reload_conversation')
        browser._click_send.assert_not_awaited()
        assert page.url == current.conversation_url
    run_dom(tmp_path, '<main><div data-message-author-role="assistant" data-message-id="previous-answer">Previous answer.</div><textarea></textarea></main>', check)
