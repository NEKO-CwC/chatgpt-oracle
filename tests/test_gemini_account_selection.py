"""Explicit account routing uses temporary state only, without any browser I/O."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserNeedsUserAction, ModelUnavailableError, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.gemini_browser import GeminiBrowser, GeminiTaskError
from chatgpt_oracle.providers import gemini_account_home


def isolated_browser(tmp_path, *, indexes=(0, 1), preferred=None):
    settings = replace(Settings.from_env(tmp_path, provider='gemini'),
        jobs_dir=tmp_path/'jobs', browser_profile_dir=tmp_path/'isolated-profile',
        shared_browser=False, gemini_account_indexes=indexes,
        gemini_preferred_account_index=preferred)
    browser = GeminiBrowser(settings)
    browser.start = AsyncMock(side_effect=AssertionError('Account preferences must not connect to Chrome'))
    return browser


def test_explicit_selection_is_durable_separate_from_automatic_and_never_opens_ui(tmp_path):
    async def run():
        browser = isolated_browser(tmp_path, preferred=1)
        browser._accounts.write({'observed_routes': {'manual-tab': 1}, 'preferred_index': 1,
                                 'selection_source': 'manual_browser_route'})
        browser._pages_by_target = AsyncMock(side_effect=AssertionError('No page inspection during selection'))
        original = 'https://gemini.google.com/u/1/app/existing-conversation'
        browser._operation.account_index = 1
        browser._operation.conversation_url = original
        result = await browser.select_account(0)
        assert result == {
            'account_index': 0, 'home_url': gemini_account_home(0), 'configured_indexes': [0, 1],
            'mode': 'explicit', 'strict': True, 'fallback_allowed': False,
            'applies_to': 'new_sessions', 'existing_sessions_unchanged': True,
            'context_rebuild_action': 'compact_or_reassemble_context_then_submit_new_session',
            'automatic_replay': False, 'account_availability_verified': False,
        }
        assert await browser._account_order() == [0]
        state = browser._accounts.read()
        assert state['preferred_index'] == 1
        assert state['observed_routes'] == {'manual-tab': 1}
        assert state['selection_source'] == 'manual_browser_route'
        assert state['explicit_selection']['account_index'] == 0
        assert state['explicit_selection']['strict'] is True
        assert state['explicit_selection']['selected_at']
        assert browser._operation.account_index == 1
        assert browser._operation.conversation_url == original
        restarted = isolated_browser(tmp_path, preferred=1)
        assert await restarted._account_order() == [0]
        browser.start.assert_not_awaited()
        browser._pages_by_target.assert_not_awaited()
        restarted.start.assert_not_awaited()
        assert not browser.settings.browser_profile_dir.exists()
    asyncio.run(run())


def test_relaxed_selection_tries_selected_account_before_other_configured_accounts(tmp_path):
    async def run():
        browser = isolated_browser(tmp_path, indexes=(0, 1), preferred=0)
        result = await browser.select_account(1, strict=False)
        assert result['mode'] == 'explicit' and result['strict'] is False
        assert result['fallback_allowed'] is True
        assert await browser._account_order() == [1, 0]
        await browser._prefer_account(0, reason='account_failover')
        assert await browser._account_order() == [1, 0]
    asyncio.run(run())


def test_reset_restores_automatic_manual_preference_and_preserves_observations(tmp_path):
    async def run():
        browser = isolated_browser(tmp_path)
        browser._context = object()
        browser._pages_by_target = AsyncMock(return_value={
            'manual-tab': SimpleNamespace(url=gemini_account_home(1)),
        })
        assert await browser._account_order() == [1, 0]
        automatic = browser._accounts.read()
        await browser.select_account(0)
        assert await browser._account_order() == [0]
        result = await browser.select_account(None)
        assert result['account_index'] is None and result['home_url'] is None
        assert result['mode'] == 'automatic' and result['strict'] is False
        assert result['fallback_allowed'] is True
        assert 'explicit_selection' not in browser._accounts.read()
        assert browser._accounts.read() == automatic
        assert await browser._account_order() == [1, 0]
    asyncio.run(run())


@pytest.mark.parametrize('account_index', [True, False, 1.0, '1', -1, 2, [], {}])
def test_invalid_account_selection_does_not_change_persisted_state(tmp_path, account_index):
    async def run():
        browser = isolated_browser(tmp_path)
        await browser.select_account(0)
        before = browser._accounts.path.read_bytes()
        with pytest.raises(GeminiTaskError) as error:
            await browser.select_account(account_index)
        assert error.value.code == 'INVALID_ACCOUNT_INDEX'
        assert browser._accounts.path.read_bytes() == before
        browser.start.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('strict', [None, 0, 1, 'true', [], {}])
@pytest.mark.parametrize('account_index', [None, 0])
def test_strict_requires_an_actual_boolean_even_when_resetting(tmp_path, strict, account_index):
    async def run():
        browser = isolated_browser(tmp_path)
        await browser.select_account(1)
        before = browser._accounts.path.read_bytes()
        with pytest.raises(GeminiTaskError) as error:
            await browser.select_account(account_index, strict=strict)
        assert error.value.code == 'INVALID_ACCOUNT_SELECTION'
        assert browser._accounts.path.read_bytes() == before
    asyncio.run(run())


def test_explicit_selection_must_be_in_configured_accounts(tmp_path):
    async def run():
        browser = isolated_browser(tmp_path, indexes=(1,))
        with pytest.raises(GeminiTaskError) as error:
            await browser.select_account(0)
        assert error.value.code == 'INVALID_ACCOUNT_INDEX'
        assert not browser._accounts.path.exists()
        await browser.select_account(1)
        assert await browser._account_order() == [1]
    asyncio.run(run())


def test_removed_explicit_account_requires_reselection_instead_of_silent_fallback(tmp_path):
    async def run():
        browser = isolated_browser(tmp_path)
        await browser.select_account(0)
        changed = isolated_browser(tmp_path, indexes=(1,))
        with pytest.raises(GeminiTaskError) as error:
            await changed._account_order()
        assert error.value.code == 'ACCOUNT_SELECTION_UNAVAILABLE'
        await changed.select_account(None)
        assert await changed._account_order() == [1]
    asyncio.run(run())


def test_concurrent_auto_updates_and_explicit_selection_preserve_both_state_fields(tmp_path):
    async def run():
        first = isolated_browser(tmp_path)
        second = isolated_browser(tmp_path)
        await asyncio.gather(first.select_account(0), second._prefer_account(1, reason='verified_selection'))
        state = second._accounts.read()
        assert state['explicit_selection']['account_index'] == 0
        assert state['preferred_index'] == 1
        assert await second._account_order() == [0]
        first.start.assert_not_awaited()
        second.start.assert_not_awaited()
    asyncio.run(run())


def test_strict_selection_model_failure_never_silently_tries_another_account(tmp_path):
    async def run():
        browser = isolated_browser(tmp_path, preferred=1)
        await browser.select_account(0)
        page = SimpleNamespace(url=gemini_account_home(0), goto=AsyncMock())
        browser._managed_page = AsyncMock(return_value=page)
        browser._prepare_send = AsyncMock(side_effect=ModelUnavailableError('Argon', ['Flash-Lite'], 'No requested model'))
        browser._confirm_send = AsyncMock()
        browser._operation.account_index = (await browser._account_order())[0]
        with pytest.raises(ModelUnavailableError) as error:
            await browser.submit('consult', 'Never send elsewhere', model='Argon', reasoning_effort='high')
        assert browser._prepare_send.await_count == 1
        assert [item['account_index'] for item in error.value.account_attempts] == [0]
        page.goto.assert_not_awaited()
        browser._confirm_send.assert_not_awaited()
    asyncio.run(run())


def test_explicit_selection_leaves_bound_conversation_on_its_original_account(tmp_path):
    async def run():
        browser = isolated_browser(tmp_path)
        await browser.select_account(0)
        original = 'https://gemini.google.com/u/1/app/existing-conversation'
        page = SimpleNamespace(url=original, goto=AsyncMock())
        completed = SubmitReceipt(original, 1, submission_state='confirmed', account_index=1)
        browser._managed_page = AsyncMock(return_value=page)
        browser._prepare_send = AsyncMock(return_value=completed)
        browser._confirm_send = AsyncMock(return_value=completed)
        result = await browser.continue_conversation(original, 'Continue the existing account conversation')
        assert result.account_index == 1 and result.conversation_url == original
        browser._managed_page.assert_awaited_once_with(original)
        page.goto.assert_not_awaited()
        assert browser._operation.account_index == 1
        assert await browser._account_order() == [0]
    asyncio.run(run())


def test_switch_preference_preserves_an_existing_unbound_sessions_url_and_draft(tmp_path):
    from test_gemini_accounts import configure, html
    from test_gemini_selection import run_gemini_page

    async def check(browser, page):
        await configure(browser, page, available0=True)
        await browser.select_account(1)
        async with browser.operation_scope(session_id='gemini:unbound-preserved'):
            original = await browser._managed_page()
            await original.locator('.ql-editor').fill('Original account-one draft, never overwrite')
            await original.evaluate("""() => {
              const card=document.createElement('div');card.id='retained-attachment';card.textContent='retained.png';
              card.innerHTML+='<img alt="retained.png" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">';
              document.body.append(card);
            }""")
            original_url = original.url
            original_target = await browser._target_id(original)
        await browser.select_account(0)
        navigations = []
        original.on('framenavigated', lambda frame: navigations.append(frame.url))
        async with browser.operation_scope(session_id='gemini:unbound-preserved'):
            retained = await browser._managed_page()
            assert retained is original
            assert await browser._target_id(retained) == original_target
            assert browser._operation.account_index == 1
            assert retained.url == original_url == gemini_account_home(1)
            assert await retained.locator('.ql-editor').inner_text() == 'Original account-one draft, never overwrite'
            assert await retained.locator('#retained-attachment img').count() == 1
            with pytest.raises(BrowserNeedsUserAction) as error:
                await browser.submit('consult', 'Must not replace that draft', model='Argon')
            assert error.value.code == 'COMPOSER_DRAFT_PRESENT'
            assert retained.url == original_url and not navigations
            assert await retained.evaluate('sends') == 0
        async with browser.operation_scope(session_id='gemini:brand-new-selection'):
            fresh = await browser._managed_page()
            assert fresh is not original
            assert fresh.url == gemini_account_home(0)
            assert browser._operation.account_index == 0
            assert await fresh.locator('.ql-editor').inner_text() == ''
            assert original.url == original_url and not navigations
            assert await original.locator('#retained-attachment img').count() == 1
            assert await original.locator('.ql-editor').inner_text() == 'Original account-one draft, never overwrite'
    run_gemini_page(tmp_path, html(0), check, timeout=2)


@pytest.mark.parametrize('explicit_relaxed', [False, True])
def test_automatic_and_relaxed_selection_retain_unsent_model_failure_fallback(tmp_path, explicit_relaxed):
    async def run():
        browser = isolated_browser(tmp_path)
        if explicit_relaxed:
            await browser.select_account(0, strict=False)
        page = SimpleNamespace(url=gemini_account_home(0), goto=AsyncMock())
        completed = SubmitReceipt('https://gemini.google.com/u/1/app/new-turn', 0,
                                  submission_state='confirmed', account_index=1)
        browser._managed_page = AsyncMock(return_value=page)
        browser._prepare_send = AsyncMock(side_effect=[
            ModelUnavailableError('Argon', ['Flash-Lite'], 'No requested model'), completed,
        ])
        browser._require_fresh_account_page = AsyncMock()
        browser._require_account_route = AsyncMock()
        browser._confirm_send = AsyncMock(return_value=completed)
        browser._operation.account_index = (await browser._account_order())[0]
        result = await browser.submit('consult', 'Only send after a model is available', model='Argon', reasoning_effort='high')
        assert result is completed
        assert browser._prepare_send.await_count == 2
        page.goto.assert_awaited_once()
        assert page.goto.await_args.args[0] == gemini_account_home(1)
        assert browser._operation.account_switch['prompt_sent_before_switch'] is False
    asyncio.run(run())
