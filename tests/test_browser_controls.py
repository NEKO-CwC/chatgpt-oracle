from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from chatgpt_oracle.browser import (
    BrowserNeedsUserAction, BrowserPageNotReady, BrowserTaskError, ChatGPTBrowser, ModelUnavailableError,
    SubmitReceipt, _matches_interpreter_filename, default_chrome_executable,
)
from chatgpt_oracle.config import Settings
from chatgpt_oracle.scheduler import Scheduler


@asynccontextmanager
async def fixture_page():
    if os.environ.get('CHATGPT_ORACLE_TEST_CDP') == '1':
        settings=replace(Settings.from_env(Path(__file__).resolve().parents[1]),browser_launch_cdp=False)
        controller=ChatGPTBrowser(settings)
        scheduler=Scheduler(settings.jobs_dir)
        task=f'fixture_{uuid.uuid4().hex}';token=uuid.uuid4().hex
        if not scheduler.acquire_external_slot(task,os.getpid(),token):
            raise RuntimeError('Dedicated browser busy; fixture was not started')
        try:
            yield await controller._new_page()
        finally:
            await controller.close()
            scheduler.release_external_slot(task,token)
    else:
        executable=default_chrome_executable()
        if not executable:
            pytest.skip('a local Chrome or Edge executable is required')
        async with async_playwright() as playwright:
            chrome=await playwright.chromium.launch(headless=True,executable_path=str(executable))
            try:
                yield await chrome.new_page()
            finally:
                await chrome.close()


def run_page(tmp_path, body, check):
    async def run():
        async with fixture_page() as page:
            await page.route("https://chatgpt.com/**", lambda route: route.fulfill(body=body, content_type="text/html; charset=utf-8"))
            await page.goto("https://chatgpt.com/")
            browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_timeout_seconds=2,
                                             browser_profile_dir=tmp_path / 'isolated-profile'))
            browser._page = AsyncMock(return_value=page)
            browser._conversation_page = AsyncMock(return_value=page)
            await check(browser, page)
    asyncio.run(run())


def test_composer_removed_during_hydration_uses_owned_new_page_recovery(tmp_path):
    async def check(browser,page):
        browser._owned_pages[page]='fixture-owned-page'
        original=browser._require_composer
        first=True
        async def disappear_once(current,**kwargs):
            nonlocal first
            await original(current,**kwargs)
            if first:
                first=False
                await current.locator('textarea').evaluate('(element)=>element.remove()')
        browser._require_composer=disappear_once
        reload=page.reload
        page.reload=AsyncMock(wraps=reload)
        recovered=await browser._prepare_submission_page(page,needs_selection=False,can_reload=True)
        assert recovered is page
        page.reload.assert_awaited_once()
        assert await page.locator('textarea').is_visible()
        assert not browser._scope.send_attempted
    run_page(tmp_path,'<main><textarea></textarea></main>',check)


def picker(*, legacy=False, pro=True, ignore_model=False):
    labels = '["Instant","Medium","High","Extra High"' + (',"Pro"]' if pro else ']')
    trigger = 'class="__composer-pill"' if legacy else 'aria-label="选择 ChatGPT 模型"'
    menu = 'data-testid="composer-intelligence-picker-content"' if legacy else ''
    wrapper = 'data-model-reasoning-effort-slider' if legacy else 'data-model-picker-power-slider'
    control = '能力' if legacy else '强度'
    return f'''<main><div data-composer-body>
      <div contenteditable="true" role="textbox" aria-label="询问 ChatGPT"></div>
      <button id="trigger" {trigger} aria-haspopup="menu">Medium</button>
      <button aria-label="发送消息" onclick="window.sent=(window.sent||0)+1">send</button>
    </div></main>
    <div id="menu" role="menu" {menu} style="display:none">
      <div id="effortView">
        <div id="toggle" role="menuitem" aria-label="选择模型" tabindex="0"><span id="label">Medium</span></div>
        <div id="effort" role="menuitem" aria-label="{control}" tabindex="0" data-reasoning-slider="true">
          <div {wrapper}><span id="slider" role="slider" aria-hidden="true" aria-valuemin="0"
            aria-valuemax="{4 if pro else 3}" aria-valuenow="1" style="display:block;width:100px;height:20px"></span></div>
        </div>
      </div>
      <div id="models" style="display:none">
        <div role="menuitemradio" aria-checked="true" tabindex="0">Latest</div>
        <div role="menuitemradio" aria-checked="false" tabindex="0">GPT-5.6 Sol</div>
      </div>
    </div>
    <script>
      const labels={labels}; window.sent=0;
      const menu=document.querySelector('#menu'), slider=document.querySelector('#slider');
      document.querySelector('#trigger').onclick=()=>{{
        menu.style.display='block'; document.querySelector('#effortView').style.display='block';
        document.querySelector('#models').style.display='none';
      }};
      document.addEventListener('keydown',e=>{{if(e.key==='Escape')menu.style.display='none'}});
      document.querySelector('#effort').onkeydown=e=>{{
        if (!['ArrowLeft','ArrowRight'].includes(e.key)) return;
        e.preventDefault(); const now=Number(slider.getAttribute('aria-valuenow'));
        const next=Math.max(0,Math.min(labels.length-1,now+(e.key==='ArrowRight'?1:-1)));
        slider.setAttribute('aria-valuenow',String(next));
        document.querySelector('#label').textContent=labels[next];
        document.querySelector('#trigger').textContent=labels[next];
      }};
      document.querySelector('#toggle').onclick=()=>{{
        document.querySelector('#effortView').style.display='none';document.querySelector('#models').style.display='block';
      }};
      document.querySelector('#toggle').onkeydown=e=>{{if(e.key==='Enter'){{e.preventDefault();e.currentTarget.click();}}}};
      document.querySelectorAll('[role=menuitemradio]').forEach(r=>r.onclick=()=>{{
        if (!{str(ignore_model).lower()}) document.querySelectorAll('[role=menuitemradio]').forEach(x=>x.setAttribute('aria-checked',String(x===r)));
        menu.style.display='none';
      }});
      document.querySelectorAll('[role=menuitemradio]').forEach(r=>r.onkeydown=e=>{{
        if(e.key==='Enter'){{e.preventDefault();r.click();}}
      }});
    </script>'''


@pytest.mark.parametrize("legacy", [False, True])
def test_picker_discovers_restores_selects_and_verifies(tmp_path: Path, legacy: bool):
    async def check(browser, page):
        found = await browser.capabilities()
        assert found.models == ["Latest", "GPT-5.6 Sol"]
        assert found.current_reasoning_effort == {"value": 1, "label": "Medium"}
        assert found.quality_aliases == {"extra_high": "Extra High", "pro": "Pro"}
        assert await page.locator('#slider').get_attribute('aria-valuenow') == '1'
        assert not await page.locator('#menu').is_visible()
        assert await browser._select_model_option(page, "GPT-5.6 Sol") == "GPT-5.6 Sol"
        effort, value, source = await browser._select_effort(page, "extra_high")
        assert (effort, value, source) == ("Extra High", 3, "explicit")
        assert await page.locator('#trigger').inner_text() == "Extra High"
        await browser._click_send(page)
        assert await page.evaluate('window.sent') == 1
    run_page(tmp_path, picker(legacy=legacy), check)


def test_missing_pro_requires_explicit_downgrade(tmp_path: Path):
    async def check(browser, page):
        with pytest.raises(ModelUnavailableError):
            await browser._select_effort(page, "pro")
        assert await page.evaluate('window.sent') == 0
        assert await page.locator('#slider').get_attribute('aria-valuenow') == '1'
        assert await browser._select_effort(page, "pro", allow_downgrade=True) == ("Extra High", 3, "downgrade")
    run_page(tmp_path, picker(pro=False), check)


@pytest.mark.parametrize('legacy',[False,True])
def test_hidden_picker_candidates_do_not_mask_visible_picker(tmp_path: Path, legacy: bool):
    hidden='<div data-composer-body hidden><button class="__composer-pill" aria-haspopup="menu">stale</button></div>'
    async def check(browser,page):
        assert await (await browser._intelligence_trigger(page)).get_attribute('id')=='trigger'
        assert await browser._select_effort(page,'extra_high')==('Extra High',3,'explicit')
    run_page(tmp_path,hidden+picker(legacy=legacy)+hidden,check)


def test_new_page_reload_is_bounded_and_precedes_upload_or_send(tmp_path: Path):
    async def check(browser,page):
        requests=[]
        browser._owned_pages[page] = 'fixture-page'
        async def hydrated(route):
            requests.append(route.request.url)
            await route.fulfill(body=picker(),content_type='text/html; charset=utf-8')
        await page.route('https://chatgpt.com/',hydrated)
        await browser._prepare_submission_page(page,needs_selection=True,can_reload=True)
        assert requests==['https://chatgpt.com/']
        assert await page.evaluate('window.sent')==0
        assert await browser._select_effort(page,'extra_high')==('Extra High',3,'explicit')
    run_page(tmp_path,'<main><textarea></textarea></main>',check)


def test_missing_picker_fails_without_upload_or_send(tmp_path: Path):
    async def check(browser,page):
        browser.settings=replace(browser.settings,browser_timeout_seconds=0.2)
        browser._upload_files=AsyncMock()
        browser._click_send=AsyncMock()
        with pytest.raises(BrowserPageNotReady):
            await browser.submit('critic','question',quality='extra_high')
        browser._upload_files.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    run_page(tmp_path,'<main><textarea></textarea></main>',check)


def test_failed_new_page_reloads_once_without_uploading_or_sending(tmp_path: Path):
    async def check(browser,page):
        browser.settings=replace(browser.settings,browser_timeout_seconds=3)
        browser._fresh_conversation_page=AsyncMock(return_value=page)
        browser._owned_pages[page]='fixture-page'
        browser._upload_files=AsyncMock()
        browser._click_send=AsyncMock()
        browser._rebuild_submission_page=AsyncMock(side_effect=BrowserPageNotReady('页面重新加载 1 次后重建仍未就绪'))
        reload=page.reload
        page.reload=AsyncMock(wraps=reload)
        with pytest.raises(BrowserPageNotReady,match='重新加载 1 次'):
            await browser.submit('critic','question',quality='extra_high',dedicated_page=True)
        page.reload.assert_awaited_once()
        browser._rebuild_submission_page.assert_awaited_once()
        browser._upload_files.assert_not_awaited()
        browser._click_send.assert_not_awaited()
    run_page(tmp_path,'<main><textarea></textarea></main>',check)


def test_verification_challenge_never_triggers_automatic_reload(tmp_path: Path):
    async def check(browser,page):
        page.reload=AsyncMock()
        with pytest.raises(BrowserNeedsUserAction,match='verification_required'):
            await browser._prepare_submission_page(page,needs_selection=True,can_reload=True)
        page.reload.assert_not_awaited()
    run_page(tmp_path,'<main>Verify you are human</main>',check)


def test_attachment_changed_effort_is_not_silently_sent(tmp_path: Path):
    async def check(browser,page):
        async def upload(*args):
            await page.evaluate('''() => {
                document.querySelector('#slider').setAttribute('aria-valuenow','1');
                document.querySelector('#label').textContent='Medium';
                document.querySelector('#trigger').textContent='Medium';
            }''')
            return [{'filename':'archive.zip','verified':True}]
        browser._upload_files=upload
        browser._click_send=AsyncMock()
        with pytest.raises(BrowserTaskError,match='发送前档位验证失败'):
            await browser.submit('critic','read archive',quality='extra_high')
        browser._click_send.assert_not_awaited()
    run_page(tmp_path,picker(),check)


def test_unapplied_model_click_is_not_reported_as_verified(tmp_path: Path):
    async def check(browser, page):
        with pytest.raises(BrowserTaskError, match="模型选择未验证"):
            await browser._select_model_option(page, "GPT-5.6 Sol")
        assert not await page.locator('#menu').is_visible()
        assert await page.evaluate('window.sent') == 0
    run_page(tmp_path, picker(ignore_model=True), check)


@pytest.mark.parametrize('toolbar_delay', [0, 750])
def test_research_send_preserves_app_mention_and_final_quality(tmp_path: Path, toolbar_delay: int):
    body = picker() + '''
      <button aria-label="添加文件等内容" id="plus">+</button>
      <div data-mention-section-id="chatgpt-actions" id="apps" hidden>
        <button id="research">深度研究 获取详细报告</button>
      </div>
      <script>
        document.querySelector('#plus').onclick=()=>document.querySelector('#apps').hidden=false;
        document.querySelector('#research').onclick=()=>{
          const composer=document.querySelector('[contenteditable=true]');
          composer.insertAdjacentHTML('beforeend','<span contenteditable="false" app-mention-path="app://connector_openai_deep_research">深度研究</span>');
          document.querySelector('#apps').hidden=true;
          document.querySelector('#slider').setAttribute('aria-valuenow','1');
          document.querySelector('#label').textContent='Medium';
        };
        document.querySelector('[aria-label="发送消息"]').onclick=()=>{
          window.sent=(window.sent||0)+1;
          window.sentText=document.querySelector('[contenteditable=true]').innerText;
          window.sentMention=document.querySelectorAll('[app-mention-path="app://connector_openai_deep_research"]').length;
          window.sentEffort=document.querySelector('#slider').getAttribute('aria-valuenow');
          const turn=document.createElement('div');
          turn.setAttribute('data-content-search-unit-key','research-user:user');
          turn.textContent='Research the test subject';
          document.querySelector('main').append(turn);
        };
      </script>'''
    async def check(browser, page):
        # Research activation plus the final picker reread has two complete
        # menu interactions, including a deliberately delayed toolbar.
        browser.settings=replace(browser.settings,browser_timeout_seconds=4)
        if toolbar_delay:
            await page.locator('#plus').evaluate('(e,delay)=>{e.hidden=true;setTimeout(()=>e.hidden=false,delay)}', toolbar_delay)
        browser._wait_for_conversation_url = AsyncMock()
        browser._start_research_plan_if_present = AsyncMock()
        receipt = await browser.submit('research','Research the test subject',quality='pro')
        sent = await page.evaluate('({count:window.sent,text:window.sentText,mention:window.sentMention,effort:window.sentEffort})')
        assert sent == {'count':1,'text':'Research the test subject深度研究','mention':1,'effort':'4'}
        assert receipt.resolved_reasoning_effort == 'Pro'
        assert receipt.submission_state == 'confirmed'
        assert receipt.current_user_message_id == 'research-user'
    run_page(tmp_path, body, check)


def test_signed_out_composer_cannot_submit_or_discover_models(tmp_path: Path):
    body = '<main><button>登录</button><button>免费注册</button><textarea></textarea></main>'
    async def check(browser, page):
        assert await browser._page_has_composer(page)
        assert (await browser.browser_status())['logged_in'] is False
        for action in (browser.capabilities(), browser.submit('consult','do not send'), browser.continue_conversation('https://chatgpt.com/c/old','do not send')):
            with pytest.raises(BrowserNeedsUserAction, match="login_required"):
                await action
    run_page(tmp_path, body, check)


def test_waits_for_hydrated_composer_instead_of_pending_textarea(tmp_path: Path):
    body = '<main><textarea id="pending-home-input" aria-label="询问 ChatGPT"></textarea></main>'
    async def check(browser, page):
        assert await browser._page_has_composer(page) is False
        await page.evaluate('''setTimeout(()=>{
          document.querySelector('main').innerHTML='<div contenteditable="true" role="textbox">ready</div>';
        },250)''')
        await browser._require_composer(page)
        composer = await browser._find_composer(page)
        await composer.fill('hydrated input')
        assert await composer.inner_text() == 'hydrated input'
    run_page(tmp_path, body, check)


def test_current_reply_dom_ignores_hidden_surface_and_waits_for_stop(tmp_path: Path):
    body = '''<nav>正在研究 unrelated sidebar</nav>
      <main hidden><div data-chatgpt-selection-message-id="hidden"><div data-markdown-text-style="assistant-message">stale</div></div></main>
      <main><article>only a user article</article><div data-content-search-unit-key="turn:0:user">question</div>
      <div data-chatgpt-selection-message-id="old"><div data-markdown-text-style="assistant-message">old answer</div></div>
      <div data-chatgpt-selection-message-id="new"><div data-markdown-text-style="assistant-message">new answer</div></div>
      <button id="stop" aria-label="停止"></button></main>'''
    async def check(browser, page):
        receipt = SubmitReceipt(page.url, 1, current_user_message_id='turn:0',
                                assistant_message_ids_before=['old'], submission_state='confirmed')
        assert await browser._assistant_count(page) == 2
        assert (await browser.inspect(receipt)).state == "RUNNING"
        await page.locator('#stop').evaluate('(e)=>e.remove()')
        snapshot = await browser.inspect(receipt)
        assert snapshot.state == "COMPLETE"
        assert snapshot.result_text == "new answer"
        assert snapshot.progress is None
        extracted = await browser._extract_dom_conversation(page, page.url)
        assert [(m['role'],m['text']) for m in extracted['messages']] == [('user','question'),('assistant','old answer'),('assistant','new answer')]
        await page.locator('[data-chatgpt-selection-message-id]').evaluate_all('(els)=>els.forEach(e=>e.remove())')
        assert await browser._assistant_count(page) == 0
        assert (await browser.inspect(SubmitReceipt(page.url,0))).state == "NEEDS_USER_INPUT"
    run_page(tmp_path, body, check)


def test_waits_for_server_conversation_url(tmp_path: Path):
    async def check(browser, page):
        await page.evaluate('''() => {
          history.replaceState({}, '', '/c/local-chatgpt%3Atemporary');
          setTimeout(()=>history.replaceState({}, '', '/c/server-conversation-id'),150);
        }''')
        await browser._wait_for_conversation_url(page)
        assert page.url.endswith('/c/server-conversation-id')
        await page.evaluate("history.replaceState({}, '', '/c/local-chatgpt%3Atemporary')")
        browser.settings = replace(browser.settings, browser_timeout_seconds=0.1)
        with pytest.raises(BrowserTaskError, match="勿重复提交"):
            await browser._wait_for_conversation_url(page)
    run_page(tmp_path, '<main></main>', check)


def test_research_acknowledgement_is_not_a_finished_report(tmp_path: Path):
    body = '''<main>
      <div data-content-search-unit-key="research-user:user">research question</div>
      <div data-chatgpt-selection-message-id="ack"><div data-markdown-text-style="assistant-message">研究已开始</div>
      <iframe title="深度研究" srcdoc="<body></body>"></iframe></div>
    </main>'''
    async def check(browser, page):
        receipt = SubmitReceipt(page.url,0,current_user_message_id='research-user',submission_state='confirmed')
        initial = await browser.inspect(receipt)
        assert initial.state == 'RUNNING'
        assert initial.result_text is None
        frame = page.frames[1]
        await frame.set_content('<body><button aria-label="停止研究">stop</button><p>正在研究…</p></body>')
        assert (await browser.inspect(receipt)).state == 'RUNNING'
        await frame.set_content('<body><iframe srcdoc="<div role=\'status\'>Research completed</div><article data-research-report><h1>Research report</h1><p>Verified final content</p></article>"></iframe></body>')
        final = await browser.inspect(receipt)
        assert final.state == 'COMPLETE'
        assert 'Verified final content' in final.result_text
        assert '研究已开始' not in final.result_text
    run_page(tmp_path,body,check)


@pytest.mark.parametrize('actual,expected', [
    ('oracle-result.txt',True), ('oracle-result(1).txt',True), ('oracle-result(12).txt',True),
    ('another-result.txt',False), ('oracle-result(0).txt',False), ('oracle-result(1).exe',False),
])
def test_interpreter_collision_filename_preserves_identity(actual, expected):
    assert _matches_interpreter_filename('oracle-result.txt',actual) is expected
