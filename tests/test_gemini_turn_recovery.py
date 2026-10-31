"""Original-turn observation and reload safety in an isolated local Chrome."""
from __future__ import annotations

import asyncio
import html
import json
import os
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserNeedsUserAction, SubmitReceipt
from chatgpt_oracle.gemini_browser import GeminiTaskError, identify_messages, matched_turn
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from chatgpt_oracle.submission_diagnostics import submission_diagnostics
from test_gemini_selection import gemini_picker, run_gemini_page


PROMPT = 'UNIQUE_PRIVATE_PROMPT_FOR_ORIGINAL_TURN'
BASELINE = [{'role':'user','text':'earlier question','dom_id':'user-query-content-0'},
            {'role':'assistant','id':'reply-baseline','text':'earlier answer'}]


def rows(*, current=True, answer=True, predecessor='reply-baseline'):
    result=[BASELINE[0],{**BASELINE[1],'id':predecessor}]
    if current:
        result.append({'role':'user','text':PROMPT,'dom_id':'user-query-content-1'})
        if answer:
            result.append({'role':'assistant','id':'reply-current','text':'current answer'})
    return result


def receipt(*, known_answer=False):
    baseline=identify_messages(BASELINE)
    current=identify_messages(rows())
    return SubmitReceipt('https://gemini.google.com/app/isolated-turn',1,
        user_message_ids_before=[baseline[0]['id']],assistant_message_ids_before=['reply-baseline'],
        submitted_prompt_sha256=normalized_text_sha256(PROMPT),current_user_message_id=current[2]['id'],
        current_assistant_message_id='reply-current' if known_answer else None,
        submission_state='confirmed',submission_verified_at='2026-10-17T00:00:00Z',
        resolved_model='4 Argon',resolved_reasoning_effort='High')


def rendered(messages):
    result=[]
    for index,message in enumerate(messages):
        if message['role']=='user':
            result.append('<div class="conversation-container" id="container-'+str(index)+'"><user-query>'+
                '<div class="query-text" id="'+message.get('dom_id','user-query-content-'+str(index))+'">'+
                html.escape(message['text'])+'</div></user-query></div>')
        else:
            result.append('<model-response><message-content id="'+message['id']+'">'+
                html.escape(message['text'])+'</message-content><button aria-label="Good response">Good</button></model-response>')
    return ''.join(result)


def body(messages):
    return gemini_picker()+rendered(messages)


async def owned(browser,page):
    await page.evaluate("history.replaceState(null,'','/app/isolated-turn')")
    browser.start=AsyncMock()
    browser._context=page.context
    browser._operation.session_id='gemini:isolated-turn'
    target=await browser._target_id(page)
    browser._operation.pages[page]=target
    async with browser._resources.locked() as connection:
        connection.execute('INSERT INTO pages(target_id,owner_pid,retained,retain_key,session_key,session_primary) VALUES(?,?,0,?,?,1)',
                           (target,os.getpid(),'gemini:isolated-turn','gemini:isolated-turn'))
    return target


def test_known_current_answer_rebinds_across_a_changed_predecessor():
    pinned=receipt(known_answer=True)
    messages=identify_messages(rows(predecessor='reply-baseline-remounted'))
    user,replies=matched_turn(messages,pinned)
    assert user is not None and replies[-1]['id']=='reply-current'
    assert user['id']!=pinned.current_user_message_id


def test_missing_predecessor_without_known_reply_is_not_hash_only_rebound():
    pinned=receipt()
    messages=identify_messages(rows(predecessor='reply-baseline-remounted'))
    assert matched_turn(messages,pinned)==(None,[])


def test_known_reply_does_not_bind_a_visible_historical_duplicate_when_own_user_is_unmounted():
    old=identify_messages([{'role':'user','text':PROMPT}])
    pinned=receipt(known_answer=True);pinned.user_message_ids_before=[old[0]['id']]
    messages=identify_messages([{'role':'user','text':PROMPT},{'role':'assistant','id':'reply-current','text':'our answer but user is unmounted'}])
    assert matched_turn(messages,pinned)==(None,[])


def test_surviving_baseline_tail_rebinds_only_the_first_adjacent_unique_prompt():
    pinned=receipt();pinned.current_user_message_id='temporarily-derived-user-id'
    user,replies=matched_turn(identify_messages(rows()),pinned)
    assert user is not None and replies[-1]['id']=='reply-current'
    later=[*rows(),{'role':'user','text':PROMPT},{'role':'assistant','id':'human-answer','text':'human'}]
    assert matched_turn(identify_messages(later),pinned)==(None,[])


def test_inspect_observes_a_temporary_gap_without_refresh_or_terminal_error(tmp_path):
    async def check(browser,page):
        await owned(browser,page)
        await page.evaluate('html=>setTimeout(()=>document.body.insertAdjacentHTML("beforeend",html),80)',
                            rendered(rows()[2:]))
        result=await browser.inspect(receipt())
        assert result.state=='COMPLETE' and result.result_text=='current answer'
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=1.5)


def test_true_missing_identity_returns_a_recoverable_snapshot_and_private_diagnostics(tmp_path):
    async def check(browser,page):
        await owned(browser,page)
        with submission_diagnostics(browser.settings.jobs_dir,'cgo_missing_fixture','gemini'):
            result=await browser.inspect(receipt())
        assert result.state=='RECOVERABLE_ERROR'
        assert result.runtime_error_code=='GEMINI_TURN_IDENTITY_MISSING'
        data=(browser.settings.jobs_dir/'diagnostics/cgo_missing_fixture.json').read_text(encoding='utf-8')
        assert PROMPT not in data and 'earlier question' not in data and 'earlier answer' not in data
        details=json.loads(data)['events'][-1]['messages']
        assert details[0]['container_id']=='container-0' and details[0]['text_sha256']
        assert 'text' not in details[0]
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=.6)


def test_safe_reload_is_checkpointed_first_then_recovers_the_same_turn(tmp_path):
    async def check(browser,page):
        target=await owned(browser,page);events=[]
        async def route(request):
            events.append('reload')
            await request.fulfill(body=body(rows()),content_type='text/html; charset=utf-8')
        await page.route('**/*',route)
        async def prepared(action):
            assert events==[]
            assert action=='reload_gemini_conversation'
            events.append('prepared')
        pinned=receipt()
        assert await browser.recover_runtime_error(pinned,attempt=1,on_recovery_prepared=prepared) is True
        assert events==['prepared','reload']
        assert await browser._target_id(page)==target and len(page.context.pages)==1
        result=await browser.inspect(pinned)
        assert result.state=='COMPLETE' and result.result_text=='current answer'
        assert pinned.current_user_message_id==receipt().current_user_message_id
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=2)


def test_two_safe_same_page_reloads_do_not_invent_a_missing_turn(tmp_path):
    async def check(browser,page):
        await owned(browser,page);actions=[]
        async def prepared(action):actions.append(action)
        pinned=receipt()
        assert await browser.recover_runtime_error(pinned,attempt=1,on_recovery_prepared=prepared)
        with pytest.raises(GeminiTaskError) as error:
            await browser.recover_runtime_error(pinned,attempt=2,on_recovery_prepared=prepared)
        assert error.value.code=='TURN_NOT_VISIBLE'
        assert actions==['reload_gemini_conversation']*2
        with pytest.raises(GeminiTaskError) as repeated:
            await browser.recover_runtime_error(pinned,attempt=2,on_recovery_prepared=prepared)
        assert repeated.value.code=='RUNTIME_RECOVERY_ALREADY_CONSUMED'
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=1)


@pytest.mark.parametrize('changed',['draft','attachment','generation','new_user','new_answer','model'])
def test_recovery_never_refreshes_changed_or_active_user_work(tmp_path,changed):
    async def check(browser,page):
        await owned(browser,page);actions=[]
        if changed=='draft':await page.locator('.ql-editor').fill('human draft')
        if changed=='attachment':
            await page.evaluate('''() => {
              const editor=document.querySelector('rich-textarea'),root=document.createElement('input-container');
              editor.replaceWith(root);root.append(editor);
              const remove=document.createElement('button');remove.setAttribute('aria-label','Remove file human.png');root.append(remove);
            }''')
        if changed=='generation':await page.evaluate('document.body.insertAdjacentHTML("beforeend",\'<button aria-label="Stop response">Stop</button>\')')
        if changed=='new_user':await page.evaluate('html=>document.body.insertAdjacentHTML("beforeend",html)',rendered([{'role':'user','text':'human follow-up'}]))
        if changed=='new_answer':await page.evaluate('html=>document.body.insertAdjacentHTML("beforeend",html)',rendered([{'role':'assistant','id':'unknown-answer','text':'human answer'}]))
        if changed=='model':await browser._picker(page).evaluate('e=>e.setAttribute("aria-label","Open mode picker, currently Pro High")')
        async def prepared(action):actions.append(action)
        with pytest.raises(GeminiTaskError) as error:
            await browser.recover_runtime_error(receipt(),attempt=1,on_recovery_prepared=prepared)
        assert error.value.code in {'GEMINI_RECOVERY_UNSAFE','MODEL_CHANGED'}
        assert actions==[] and await page.evaluate('sends')==0
        if changed=='draft':assert await page.locator('.ql-editor').inner_text()=='human draft'
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=1)


@pytest.mark.parametrize('generating',[False,True])
def test_turn_arriving_during_checkpoint_skips_refresh_and_is_observed_next(tmp_path,generating):
    async def check(browser,page):
        await owned(browser,page);reloads=[]
        page.on('request',lambda request:reloads.append(request.url))
        async def prepared(_action):
            await page.evaluate('html=>document.body.insertAdjacentHTML("beforeend",html)',rendered(rows()[2:]))
            if generating:
                await page.evaluate('''() => {
                  document.querySelector('model-response:last-of-type button').remove();
                  document.body.insertAdjacentHTML('beforeend','<button aria-label="Stop response">Stop</button>');
                }''')
        pinned=receipt()
        assert await browser.recover_runtime_error(pinned,attempt=1,on_recovery_prepared=prepared) is True
        assert reloads==[]
        result=await browser.inspect(pinned)
        assert result.state==('RUNNING' if generating else 'COMPLETE')
        assert result.result_text==(None if generating else 'current answer')
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=1)


@pytest.mark.parametrize('challenge',['login','captcha','hidden_captcha'])
def test_only_visible_signin_or_captcha_needs_user_action(tmp_path,challenge):
    async def check(browser,page):
        await owned(browser,page)
        if challenge=='login':
            await page.evaluate('document.body.insertAdjacentHTML("beforeend",\'<button>Sign in</button>\')')
        else:
            await page.evaluate('hidden=>{const f=document.createElement("iframe");f.src="about:blank#recaptcha";if(hidden)f.style.display="none";document.body.append(f);}',challenge=='hidden_captcha')
        if challenge=='hidden_captcha':
            assert await browser._ready(page)
        else:
            with pytest.raises(BrowserNeedsUserAction) as error:
                await browser._ready(page)
            assert error.value.code==('LOGIN_REQUIRED' if challenge=='login' else 'VERIFICATION_REQUIRED')
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=1)


def test_cancelled_checkpoint_has_no_browser_action(tmp_path):
    async def check(browser,page):
        await owned(browser,page);reloads=[]
        page.on('request',lambda request:reloads.append(request.url))
        async def cancelled(_action):raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await browser.recover_runtime_error(receipt(),attempt=1,on_recovery_prepared=cancelled)
        assert reloads==[] and await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=1)


def test_inspection_does_not_create_a_replacement_page_for_missing_ownership(tmp_path):
    async def check(browser,page):
        browser.start=AsyncMock();browser._context=page.context
        browser._operation.session_id='gemini:unavailable'
        with pytest.raises(GeminiTaskError) as error:
            await browser.inspect(receipt())
        assert error.value.code=='TURN_NOT_VISIBLE' and len(page.context.pages)==1
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,body(BASELINE),check,timeout=1)
