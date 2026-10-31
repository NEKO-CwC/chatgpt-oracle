"""Multi-account browser behavior, using only isolated local HTML routes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import ModelUnavailableError, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.gemini_browser import GeminiTaskError
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from chatgpt_oracle.providers import gemini_account_index
from test_gemini_selection import gemini_picker, run_gemini_page


def html(index, *, available=True, quota=False):
    body = gemini_picker(initial_model='4 Argon' if available else '3.5 Flash-Lite', delay=0, thinking_delay=0)
    if not available:
        body = body.replace("['3.5 Flash-Lite','3.8 Flash','3.1 Pro','4 Argon']", "['3.5 Flash-Lite']")
    return body + ('<div role="alert">Account usage limit reached</div>' if quota else '') + r'''
    <script>
    document.querySelector('[aria-label="Send message"]').addEventListener('click',()=>{
      const editor=document.querySelector('.ql-editor'),user=document.createElement('user-query');
      const query=document.createElement('div');query.className='query-text';query.textContent=editor.innerText;
      user.append(query);document.body.append(user);editor.innerText='';
      const answer=document.createElement('model-response'),content=document.createElement('message-content');
      content.id='reply-ACCOUNT-'+window.sends;content.textContent='ANSWER ACCOUNT';answer.append(content);
      answer.insertAdjacentHTML('beforeend','<button aria-label="Good response">Good</button>');document.body.append(answer);
      history.replaceState({},'','/u/ACCOUNT/app/account-test-ACCOUNT');
    });
    </script>'''.replace('ACCOUNT', str(index))


async def configure(browser, page, *, available0=False, available1=True):
    browser.settings = replace(browser.settings, gemini_account_indexes=(0, 1))
    browser.start = AsyncMock()
    browser._context = page.context
    bodies = {0: html(0, available=available0), 1: html(1, available=available1)}

    async def route(request):
        index = gemini_account_index(request.request.url)
        await request.fulfill(body=bodies[index], content_type='text/html; charset=utf-8')

    await page.context.route('**/*', route)
    return bodies


def test_new_unsent_conversation_tries_other_account_and_sends_once(tmp_path):
    async def check(browser, page):
        await configure(browser, page)
        uploads = []
        async def upload(target, inputs):
            uploads.append((gemini_account_index(target.url), inputs))
            return []
        browser._upload = upload
        async with browser.operation_scope(session_id='gemini:account-test'):
            result = await browser.submit('consult', 'Only send this once', model='Argon', reasoning_effort='high',
                                          attachments=['task-pinned-original-input'])
            assert result.submission_state == 'confirmed'
            assert result.conversation_url.startswith('https://gemini.google.com/u/1/app/')
            assert result.account_index == 1
            assert result.account_switch['requires_context_rebuild'] is False
            assert [(x['account_index'], x['state']) for x in result.account_attempts] == [(0, 'unavailable'), (1, 'ready')]
            assert uploads == [(1, ['task-pinned-original-input'])]
            assert await page.evaluate('sends') == 0
            target = next(p for p in page.context.pages if '/u/1/app/account-test' in p.url)
            assert await target.evaluate('sends') == 1
            assert (await browser.inspect(result)).state == 'COMPLETE'
            assert len(page.context.pages) == 2
    run_gemini_page(tmp_path, html(0, available=False), check, timeout=2)


def test_existing_conversation_switch_notifies_client_and_preserves_original(tmp_path):
    async def check(browser, page):
        await configure(browser, page, available0=True)
        async with browser.operation_scope(session_id='gemini:existing'):
            original = await browser.submit('consult', 'Original context', model='Argon', reasoning_effort='high')
            target = next(p for p in page.context.pages if '/u/0/app/account-test' in p.url)
            await target.evaluate('document.body.insertAdjacentHTML("beforeend",\'<div role="alert">Account usage limit reached</div>\')')
            uploads = AsyncMock(return_value=[])
            browser._upload = uploads
            with pytest.raises(GeminiTaskError) as caught:
                await browser.continue_conversation(original.conversation_url, 'Unsent follow-up', model='Argon', reasoning_effort='high')
            assert caught.value.code == 'ACCOUNT_CONTEXT_REQUIRED'
            switch = caught.value.account_switch
            assert switch['from_account_index'] == 0 and switch['to_account_index'] == 1
            assert switch['requires_context_rebuild'] is True and switch['send_attempted'] is False
            assert switch['automatic_replay'] is False
            assert switch['original_conversation_url'] == original.conversation_url
            assert target.url == original.conversation_url
            assert await target.evaluate('sends') == 1
            assert await target.locator('.ql-editor').inner_text() == ''
            uploads.assert_not_awaited()
            assert len(page.context.pages) == 2  # Temporary probe released.
        async with browser.operation_scope(session_id='gemini:client-rebuilt'):
            rebuilt = await browser.submit('consult', 'Client rebuilt context and new question', model='Argon', reasoning_effort='high')
            assert rebuilt.account_index == 1
            assert rebuilt.user_message_ids_before == []
            assert rebuilt.submission_state == 'confirmed'
    run_gemini_page(tmp_path, html(0), check, timeout=2)


def test_confirmed_turn_never_replays_when_quota_is_reported_after_send(tmp_path):
    async def check(browser, page):
        await configure(browser, page, available0=True)
        async with browser.operation_scope(session_id='gemini:pending-quota'):
            original = await browser.submit('consult', 'Original context', model='Argon', reasoning_effort='high')
            target = next(p for p in page.context.pages if '/u/0/app/account-test' in p.url)
            await target.evaluate('''() => {
              document.querySelector('model-response').remove();
              document.body.insertAdjacentHTML('beforeend','<div role="alert">Account usage limit reached</div>');
            }''')
            original.current_assistant_message_id = None
            with pytest.raises(GeminiTaskError) as caught:
                await browser.inspect(original)
            assert caught.value.code == 'ACCOUNT_CONTEXT_REQUIRED'
            assert caught.value.account_switch['send_attempted'] is True
            assert await target.evaluate('sends') == 1
            assert len(page.context.pages) == 2
    run_gemini_page(tmp_path, html(0), check, timeout=2)


def test_all_accounts_unavailable_are_bounded_and_do_not_fill_or_send(tmp_path):
    async def check(browser, page):
        await configure(browser, page, available1=False)
        browser._upload = AsyncMock(side_effect=AssertionError('No upload before a usable account'))
        async with browser.operation_scope(session_id='gemini:all-limited'):
            with pytest.raises(ModelUnavailableError) as caught:
                await browser.submit('consult', 'Must stay unsent', model='Argon', reasoning_effort='high')
            assert len(caught.value.account_attempts) == 2
            assert caught.value.account_switch is None
            for target in page.context.pages:
                assert await target.evaluate('sends') == 0
                assert await target.locator('.ql-editor').inner_text() == ''
            browser._upload.assert_not_awaited()
    run_gemini_page(tmp_path, html(0, available=False), check, timeout=2)


def test_manual_route_change_updates_preference_without_changing_others(tmp_path):
    async def check(browser, page):
        await configure(browser, page)
        assert await browser._account_order() == [0, 1]
        await page.goto('https://gemini.google.com/u/1/app?pli=1')
        assert await browser._account_order() == [1, 0]
        caps = await browser.capabilities()
        assert caps.account_index == 1 and caps.current_model == '4 Argon'
        assert page.url == 'https://gemini.google.com/u/1/app?pli=1'
        assert len(page.context.pages) == 1
    run_gemini_page(tmp_path, html(0, available=False), check, timeout=2)


def test_account_switch_metadata_is_durable_and_public(tmp_path):
    async def run():
        settings = replace(Settings.from_env(tmp_path, provider='gemini'), shared_browser=False,
                           browser_profile_dir=tmp_path/'profile', consult_poll_seconds=.01)
        switch = {'from_account_index': 0, 'to_account_index': 1, 'requires_context_rebuild': True,
                  'target_home_url': 'https://gemini.google.com/u/1/app?pli=1', 'automatic_replay': False}
        class Fake:
            requires_turn_receipts = True
            async def submit(self, *args, **kwargs):
                error = GeminiTaskError('ACCOUNT_CONTEXT_REQUIRED', 'Client must rebuild context')
                error.account_index = 0
                error.account_attempts = [{'account_index': 0}, {'account_index': 1}]
                error.account_switch = switch
                raise error
            async def close(self): pass
        manager = JobManager(settings, Fake())
        job = Job.create('consult', 'Unsent', session_id='metadata')
        manager.store.save(job)
        try:
            await manager._process_body(job)
            saved = manager.store.get(job.id)
            assert saved.state == 'ERROR' and saved.error_code == 'ACCOUNT_CONTEXT_REQUIRED'
            assert saved.account_switch == manager.public(job.id)['account_switch'] == switch
            assert saved.account_index == 0 and len(saved.account_attempts) == 2
            reloaded = Job.from_dict(saved.as_dict())
            assert reloaded.account_switch == switch
            assert saved.result_path is None and saved.submission_state == 'legacy_unverified'
        finally:
            await manager.stop()
    asyncio.run(run())


def test_manual_account_change_on_a_retained_page_preserves_its_draft(tmp_path):
    async def check(browser, page):
        await configure(browser, page, available0=True)
        async with browser.operation_scope(session_id='gemini:manually-moved'):
            original = await browser.submit('consult', 'Original context', model='Argon', reasoning_effort='high')
            target = next(p for p in page.context.pages if '/u/0/app/account-test' in p.url)
            await target.goto('https://gemini.google.com/u/1/app?pli=1')
            await target.locator('.ql-editor').fill('Human draft on the newly selected account')
            with pytest.raises(GeminiTaskError) as caught:
                await browser.continue_conversation(original.conversation_url, 'Do not put this in the human draft',
                                                    model='Argon', reasoning_effort='high')
            assert caught.value.code == 'ACCOUNT_CONTEXT_REQUIRED'
            assert caught.value.account_switch['to_account_index'] == 1
            assert target.url == 'https://gemini.google.com/u/1/app?pli=1'
            assert await target.locator('.ql-editor').inner_text() == 'Human draft on the newly selected account'
            assert await target.evaluate('sends') == 0
    run_gemini_page(tmp_path, html(0), check, timeout=2)


def test_menu_missing_model_on_first_read_is_rechecked_on_same_account(tmp_path):
    async def run():
        from chatgpt_oracle.gemini_browser import GeminiBrowser
        browser = GeminiBrowser(replace(Settings.from_env(tmp_path, provider='gemini'), browser_profile_dir=tmp_path/'profile'))
        available = [{'name': '4 Argon', 'disabled': False}]
        high = {'label': 'High', 'value': 2}
        browser._options = AsyncMock(side_effect=[
            ([{'name': '3.5 Flash-Lite', 'disabled': False}], '3.5 Flash-Lite', [], None),
            (available, '4 Argon', [], None), (available, '4 Argon', [high], high),
        ])
        result = await browser._selection(object(), 'Argon', 'high', 'high', False)
        assert result['resolved_model'] == '4 Argon'
        assert browser._options.await_count == 3
        assert browser._operation.account_switch is None
    asyncio.run(run())


def test_send_confirmation_rejects_other_account_even_when_prompt_matches(tmp_path):
    async def run():
        from chatgpt_oracle.gemini_browser import GeminiBrowser
        browser = GeminiBrowser(replace(Settings.from_env(tmp_path, provider='gemini'), browser_profile_dir=tmp_path/'profile'))
        original = SubmitReceipt('https://gemini.google.com/u/1/app?pli=1', 0,
            account_index=1, submitted_prompt_sha256=normalized_text_sha256('Original body'), submission_state='unconfirmed')
        browser._surface = AsyncMock(side_effect=AssertionError('Wrong-account content must not be matched'))
        with pytest.raises(GeminiTaskError) as caught:
            await browser._confirm_send(SimpleNamespace(url='https://gemini.google.com/u/0/app/the-same-id'), original)
        assert caught.value.code == 'ACCOUNT_ROUTE_MISMATCH'
        browser._surface.assert_not_awaited()
    asyncio.run(run())


def test_old_and_explicit_zero_aliases_match_but_other_accounts_do_not(tmp_path):
    async def run():
        manager = JobManager(replace(Settings.from_env(tmp_path, provider='gemini'), shared_browser=False,
                                    browser_profile_dir=tmp_path/'profile'))
        try:
            assert manager._conversation_key('https://gemini.google.com/app/same-id') == manager._conversation_key('https://gemini.google.com/u/0/app/same-id')
            assert manager._conversation_key('https://gemini.google.com/u/1/app/same-id') != manager._conversation_key('https://gemini.google.com/u/0/app/same-id')
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize('indices,preferred', [('0,1', '1'), ('1,0', '0')])
def test_account_configuration(monkeypatch, tmp_path, indices, preferred):
    monkeypatch.setenv('GEMINI_ACCOUNT_INDEXES', indices)
    monkeypatch.setenv('GEMINI_PREFERRED_ACCOUNT_INDEX', preferred)
    value = Settings.from_env(tmp_path, provider='gemini')
    assert value.gemini_account_indexes == tuple(int(i) for i in indices.split(','))
    assert value.gemini_preferred_account_index == int(preferred)


@pytest.mark.parametrize('indices,preferred', [('0,-1', ''), ('0,1', '2'), ('', ''), ('0,1', 'x')])
def test_invalid_account_configuration(monkeypatch, tmp_path, indices, preferred):
    monkeypatch.setenv('GEMINI_ACCOUNT_INDEXES', indices)
    monkeypatch.setenv('GEMINI_PREFERRED_ACCOUNT_INDEX', preferred)
    with pytest.raises(ValueError):
        Settings.from_env(tmp_path, provider='gemini')
