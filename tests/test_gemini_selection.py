"""Real local DOM, an isolated headless Chrome, and no upstream requests."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from chatgpt_oracle.browser import BrowserNeedsUserAction
from chatgpt_oracle.config import Settings, default_chrome_executable
from chatgpt_oracle.gemini_browser import GeminiBrowser, GeminiTaskError
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from chatgpt_oracle.submission_diagnostics import submission_diagnostics


def gemini_picker(*, initial_model="4 Argon", initial_effort="High", delay=80,
                  thinking_delay=100, overlay=None, missing_effort=False,
                  disabled_pro=False, ignore_effort=False):
    config = json.dumps(dict(initial_model=initial_model, initial_effort=initial_effort,
        delay=delay, thinking_delay=thinking_delay, overlay=overlay,
        missing_effort=missing_effort, disabled_pro=disabled_pro, ignore_effort=ignore_effort))
    return r'''<!doctype html><meta charset="utf-8">
    <rich-textarea><div class="ql-editor" contenteditable="true" role="textbox"></div></rich-textarea>
    <button id="picker" data-test-id="bard-mode-menu-button" aria-expanded="false" aria-controls="models">Argon High</button>
    <button aria-label="Send message" onclick="window.sends++">Send</button>
    <div id="models" role="menu" style="display:none"></div>
    <script>
    const config=CONFIG;
    window.sends=0;window.modelClicks=0;window.effortClicks=0;window.badToggle=0;
    let model=config.initial_model,effort=config.initial_effort,closing=false;
    const picker=document.querySelector('#picker'),root=document.querySelector('#models');
    function label(){picker.setAttribute('aria-label','Open mode picker, currently '+model.replace(/^[\d.]+\s+/,'')+' '+effort);}
    function closeRoot(){
      if(closing)return;closing=true;
      setTimeout(()=>{root.style.display='none';picker.setAttribute('aria-expanded','false');closing=false;},config.delay);
    }
    function render(){
      root.innerHTML='';
      for(const name of ['3.5 Flash-Lite','3.8 Flash','3.1 Pro','4 Argon']){
        const item=document.createElement('div');item.setAttribute('role','menuitem');item.tabIndex=0;
        item.textContent=name+' '+(name.includes('Argon')?'Complex tasks':'Description');
        item.classList.toggle('selected',name===model);
        item.setAttribute('aria-disabled',String(config.disabled_pro && name==='3.1 Pro'));
        item.onclick=()=>{
          window.modelClicks++;
          setTimeout(()=>{model=name;effort='Low';label();},config.delay/2);
          closeRoot();
        };root.append(item);
      }
      if(model.includes('Argon') && !config.missing_effort){
        const thinking=document.createElement('div');thinking.setAttribute('role','menuitem');thinking.tabIndex=0;
        thinking.textContent='Thinking level';thinking.setAttribute('aria-controls','efforts');thinking.setAttribute('aria-expanded','false');
        thinking.style.display='none';root.append(thinking);
        const child=document.createElement('div');child.id='efforts';child.setAttribute('role','menu');child.style.display='none';root.append(child);
        for(const name of ['Low','Medium','High']){
          const item=document.createElement('div');item.setAttribute('role','menuitem');item.tabIndex=0;
          item.textContent=name+' '+(name==='High'?'Extra thorough':'Description');item.classList.toggle('selected',name===effort);
          item.onclick=()=>{
            window.effortClicks++;
            setTimeout(()=>{if(!config.ignore_effort)effort=name;label();},config.delay/2);
            closeRoot();
          };child.append(item);
        }
        thinking.onclick=()=>{child.style.display='block';thinking.setAttribute('aria-expanded','true');};
        setTimeout(()=>{thinking.style.display='block';},config.thinking_delay);
      }
    }
    picker.onclick=()=>{
      if(closing){window.badToggle++;return;}
      if(picker.getAttribute('aria-expanded')==='true'){closeRoot();return;}
      render();root.style.display='block';picker.setAttribute('aria-expanded','true');
    };
    document.addEventListener('keydown',e=>{
      if(e.key!=='Escape')return;
      const child=document.querySelector('#efforts');
      if(child && child.style.display==='block'){
        setTimeout(()=>{child.style.display='none';root.querySelector('[aria-controls="efforts"]').setAttribute('aria-expanded','false');},config.delay);
      }else if(picker.getAttribute('aria-expanded')==='true'){closeRoot();}
    });
    if(config.overlay){
      const container=document.createElement('div');container.className='cdk-overlay-container';
      container.innerHTML='<div class="cdk-overlay-backdrop" style="position:fixed;inset:0;z-index:10;background:#0002"></div>'+
        '<div class="cdk-overlay-pane" style="position:fixed;top:5px;left:5px;z-index:11">'+
        (config.overlay==='known'?'<gem-discovery-tooltip>Hand off your to-dos</gem-discovery-tooltip>':'<div role="dialog">Manual attention</div>')+'</div>';
      document.body.append(container);
      if(config.overlay==='known')container.querySelector('.cdk-overlay-backdrop').onclick=()=>container.remove();
    }
    label();
    </script>'''.replace('CONFIG', config)


def run_gemini_page(tmp_path, body, check, *, timeout=3):
    async def run():
        executable = default_chrome_executable()
        if not executable:
            pytest.skip("local Chrome/Edge is unavailable")
        async with async_playwright() as pw:
            chrome = await pw.chromium.launch(headless=True, executable_path=str(executable))
            try:
                context = await chrome.new_context()
                await context.route('**/*', lambda route: route.fulfill(
                    body=body, content_type='text/html; charset=utf-8'))
                page = await context.new_page()
                page.set_default_timeout(timeout * 1000)
                await page.goto('https://gemini.google.com/app')
                settings = replace(Settings.from_env(tmp_path, provider='gemini'),
                    gemini_account_indexes=(0,),
                    browser_profile_dir=tmp_path/'isolated-profile', browser_timeout_seconds=timeout)
                browser = GeminiBrowser(settings)
                await check(browser, page)
            finally:
                await chrome.close()
    asyncio.run(run())


def hydrating_picker(*, keep_stale_menu=False):
    """A cold Angular placeholder is replaced after its first picker click."""
    return r'''<!doctype html><meta charset="utf-8">
    <rich-textarea><div class="ql-editor" contenteditable="true" role="textbox"></div></rich-textarea>
    <button id="picker" aria-label="Open mode picker, currently Argon High"
      aria-controls="models-placeholder" aria-expanded="false">Argon High</button>
    <button aria-label="Send message" onclick="window.sends++">Send</button>
    <div id="models-placeholder" role="menu" style="display:none;min-height:1px"></div>
    <script>
    window.sends=0;window.placeholderClicks=0;window.readyClicks=0;
    let hydrated=false,picker=document.querySelector('#picker'),root=document.querySelector('#models-placeholder');
    function render(){
      root.innerHTML='<div role="menuitem" class="selected">4 Argon Complex tasks</div>'+
        '<div role="menuitem">3.1 Pro Description</div>'+
        '<div role="menuitem" tabindex="0" aria-controls="efforts-ready" aria-expanded="false">Thinking level</div>'+
        '<div id="efforts-ready" role="menu" style="display:none">'+
          '<div role="menuitem">Low Description</div><div role="menuitem">Medium Description</div>'+
          '<div role="menuitem" class="selected">High Extra thorough</div></div>';
      const thinking=root.querySelector('[aria-controls]');
      thinking.onclick=()=>{thinking.setAttribute('aria-expanded','true');root.querySelector('#efforts-ready').style.display='block';};
    }
    function open(){
      if(!hydrated){
        window.placeholderClicks++;root.style.display='block';picker.setAttribute('aria-expanded','true');
        setTimeout(()=>{
          const old=root,oldPicker=picker;
          picker=oldPicker.cloneNode(true);picker.setAttribute('aria-controls','models-ready');picker.setAttribute('aria-expanded','false');
          oldPicker.replaceWith(picker);
          root=document.createElement('div');root.id='models-ready';root.setAttribute('role','menu');root.style.display='none';document.body.append(root);
          if(!KEEP_STALE)old.remove();
          hydrated=true;picker.onclick=open;
        },90);
        return;
      }
      window.readyClicks++;
      if(picker.getAttribute('aria-expanded')==='true'){root.style.display='none';picker.setAttribute('aria-expanded','false');return;}
      render();root.style.display='block';picker.setAttribute('aria-expanded','true');
    }
    picker.onclick=open;
    document.addEventListener('keydown',event=>{
      if(event.key!=='Escape')return;
      const child=root.querySelector('#efforts-ready');
      if(child?.style.display==='block'){child.style.display='none';root.querySelector('[aria-controls]').setAttribute('aria-expanded','false');}
      else{root.style.display='none';picker.setAttribute('aria-expanded','false');}
    });
    </script>'''.replace('KEEP_STALE', json.dumps(keep_stale_menu))


@pytest.mark.parametrize('keep_stale_menu', [False, True])
def test_cold_picker_reresolves_replaced_trigger_and_control_id(tmp_path, keep_stale_menu):
    async def check(browser, page):
        receipt=await browser._selection(page,'Argon','high',None,False)
        assert receipt['resolved_model']=='4 Argon'
        assert receipt['resolved_reasoning_effort']=='High'
        assert await page.evaluate('({sends,placeholderClicks,readyClicks})')==dict(sends=0,placeholderClicks=1,readyClicks=2)
        assert await browser._picker(page).get_attribute('aria-expanded')=='false'
        if keep_stale_menu:
            assert await page.locator('#models-placeholder').is_visible()
            assert not await page.locator('#models-placeholder [role="menuitem"]').count()
    run_gemini_page(tmp_path,hydrating_picker(keep_stale_menu=keep_stale_menu),check,timeout=.8)


def test_thinking_menu_reresolves_changed_control_with_visible_stale_menu(tmp_path):
    async def check(browser,page):
        menu=await browser._open_picker(page)
        await page.evaluate(r'''() => {
          const thinking=document.querySelector('[aria-controls="efforts"]');
          let hydrated=false;
          thinking.onclick=()=>{
            if(hydrated){
              document.querySelector('#efforts-hydrated').style.display='block';
              thinking.setAttribute('aria-expanded','true');return;
            }
            const old=document.querySelector('#efforts'),html=old.innerHTML;
            old.innerHTML='';old.style.cssText='display:block;min-height:1px';thinking.setAttribute('aria-expanded','true');
            setTimeout(()=>{
              const child=document.createElement('div');child.id='efforts-hydrated';child.setAttribute('role','menu');
              child.innerHTML=html;child.style.display='none';old.after(child);
              thinking.setAttribute('aria-controls',child.id);thinking.setAttribute('aria-expanded','false');hydrated=true;
            },90);
          };
        }''')
        current=await browser._thinking_menu(page,menu)
        assert await current.get_attribute('id')=='efforts-hydrated'
        assert any(row['selected'] and row['label'].startswith('High') for row in await browser._read_menu(current))
        assert await page.locator('#efforts').is_visible()
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,gemini_picker(thinking_delay=0),check,timeout=.8)


def test_collapsed_picker_reopens_are_bounded_and_never_send(tmp_path):
    async def check(browser,page):
        await page.evaluate('() => {window.opens=0;document.querySelector("#picker").onclick=()=>window.opens++;}')
        with pytest.raises(GeminiTaskError,match='current controlled menu'):
            await browser._selection(page,'Argon','high',None,False)
        assert await page.evaluate('opens')==3
        assert await page.evaluate('sends')==0
        assert not (await page.locator('.ql-editor').inner_text()).strip()
    run_gemini_page(tmp_path,gemini_picker(),check,timeout=.9)


def test_existing_selection_is_not_reselected_and_delayed_effort_is_observed(tmp_path):
    async def check(browser, page):
        receipt = await browser._selection(page, 'Argon', 'high', 'high', False)
        assert receipt['resolved_model'] == '4 Argon'
        assert receipt['resolved_reasoning_effort'] == 'High'
        assert await page.evaluate('({models:modelClicks,efforts:effortClicks,sends,badToggle})') == dict(models=0,efforts=0,sends=0,badToggle=0)
        assert await browser._picker(page).get_attribute('aria-expanded') == 'false'
    run_gemini_page(tmp_path, gemini_picker(thinking_delay=250), check)


def test_model_switch_and_effort_reset_wait_for_closed_overlays(tmp_path):
    async def check(browser, page):
        receipt = await browser._selection(page, 'Argon', 'high', None, False)
        assert receipt['resolved_model'] == '4 Argon' and receipt['resolved_reasoning_effort'] == 'High'
        assert await page.evaluate('({models:modelClicks,efforts:effortClicks,sends,badToggle})') == dict(models=1,efforts=1,sends=0,badToggle=0)
        assert not await page.locator('[role="menu"]:visible').count()
    run_gemini_page(tmp_path, gemini_picker(initial_model='3.8 Flash',delay=140), check)


def test_known_discovery_is_dismissed_normally(tmp_path):
    async def check(browser, page):
        assert (await browser._selection(page,'Argon','high',None,False))['resolved_reasoning_effort']=='High'
        assert not await page.locator('.cdk-overlay-backdrop').count()
        assert await page.evaluate('sends') == 0
    run_gemini_page(tmp_path,gemini_picker(overlay='known'),check)


def test_unknown_dialog_and_existing_draft_are_preserved(tmp_path):
    async def check(browser,page):
        await page.locator('.ql-editor').fill('human draft')
        with pytest.raises(BrowserNeedsUserAction):
            await browser._selection(page,'Argon','high',None,False)
        assert await page.get_by_role('dialog').is_visible()
        assert await page.locator('.ql-editor').inner_text() == 'human draft'
        assert await page.evaluate('sends') == 0
    run_gemini_page(tmp_path,gemini_picker(overlay='unknown'),check)


def test_failure_captures_menu_phase_without_prompt_and_closes_owned_menu(tmp_path):
    async def check(browser,page):
        with submission_diagnostics(browser.settings.jobs_dir,'cgo_fixture','gemini'):
            with pytest.raises(Exception):
                await browser._selection(page,'Argon','high',None,False)
        data=json.loads((browser.settings.jobs_dir/'diagnostics/cgo_fixture.json').read_text(encoding='utf-8'))
        failure=next(e for e in data['events'] if e['phase']=='selection_failed')
        assert failure['stage']=='effort_menu_opening'
        assert data['send_attempted'] is False and failure['surface']['menus']
        assert not await page.locator('[role="menu"]:visible').count()
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,gemini_picker(missing_effort=True),check,timeout=.6)


def test_effort_readback_failure_never_reaches_prompt_fill_or_send(tmp_path):
    async def check(browser,page):
        browser._managed_page=AsyncMock(return_value=page)
        with pytest.raises(GeminiTaskError,match='read back'):
            await browser.submit('consult','must not enter composer',model='Argon',quality='high')
        assert not (await page.locator('.ql-editor').inner_text()).strip()
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,gemini_picker(initial_effort='Low',ignore_effort=True),check)


def test_local_first_turn_and_continuation_confirm_unique_messages(tmp_path):
    async def check(browser,page):
        browser._managed_page=AsyncMock(return_value=page)
        await page.evaluate(r'''() => {
          document.querySelector('[aria-label="Send message"]').addEventListener('click',()=>{
            const editor=document.querySelector('.ql-editor'),text=editor.innerText;
            const user=document.createElement('user-query'),query=document.createElement('div');
            query.className='query-text';query.textContent=text;user.append(query);document.body.append(user);
            const response=document.createElement('model-response'),content=document.createElement('message-content');
            content.id='reply-'+window.sends;content.textContent='Local fixture answer';
            const good=document.createElement('button');good.setAttribute('aria-label','Good response');
            response.append(content,good);document.body.append(response);editor.innerText='';
            history.replaceState(null,'','/app/local-fixture');
          });
        }''')
        first=await browser.submit('consult','local first prompt',model='Argon',quality='high')
        second=await browser.continue_conversation(first.conversation_url,'local continuation',model='Argon',quality='high')
        assert first.submission_state==second.submission_state=='confirmed'
        assert first.current_user_message_id!=second.current_user_message_id
        assert first.current_assistant_message_id=='reply-1' and second.current_assistant_message_id=='reply-2'
        assert first.resolved_reasoning_effort==second.resolved_reasoning_effort=='High'
        assert await page.evaluate('sends')==2
        assert await page.locator('user-query').count()==2
    run_gemini_page(tmp_path,gemini_picker(),check)


def test_selection_change_during_upload_stops_before_prompt_or_send(tmp_path):
    async def check(browser,page):
        browser._managed_page=AsyncMock(return_value=page)
        async def upload(*_):
            await page.evaluate("() => {model='3.8 Flash';effort='Low';label();}")
            return [{'verified':True,'filename':'fixture.png'}]
        browser._upload=upload
        with pytest.raises(GeminiTaskError) as error:
            await browser.submit('consult','must not fill',model='Argon',quality='high')
        assert error.value.code=='MODEL_CHANGED'
        assert not (await page.locator('.ql-editor').inner_text()).strip()
        assert await page.evaluate('sends')==0
    run_gemini_page(tmp_path,gemini_picker(),check)


def test_initial_send_confirmation_keeps_ui_ownership_but_generation_does_not(tmp_path):
    from chatgpt_oracle.browser import SubmitReceipt
    from chatgpt_oracle.browser_resources import ProfileResources, UIAdmissionDeferred
    async def run():
        value=replace(Settings.from_env(tmp_path,provider='gemini'),browser_profile_dir=tmp_path/'profile',browser_timeout_seconds=.05)
        browser=GeminiBrowser(value)
        page=object()
        receipt=SubmitReceipt('https://gemini.google.com/app/local-test',0,submission_state='confirmed')
        browser._managed_page=AsyncMock(return_value=page)
        browser._prepare_send=AsyncMock(return_value=receipt)
        entered,release=asyncio.Event(),asyncio.Event()
        async def confirm(*_):
            entered.set();await release.wait();return receipt
        browser._confirm_send=confirm
        task=asyncio.create_task(browser.submit('consult','unique test question'))
        try:
            await entered.wait()
            peer=ProfileResources(value.browser_profile_dir,lock_timeout_seconds=.02)
            with pytest.raises(UIAdmissionDeferred):
                async with peer.ui_admitted():pass
            release.set();assert await task is receipt
            async with peer.ui_admitted():pass
        finally:
            release.set();await task
    asyncio.run(run())


def test_file_ref_upload_path_survives_preview_return_for_late_browser_reads(tmp_path):
    from chatgpt_oracle.file_store import FileRecord
    import hashlib,time
    async def run():
        source=tmp_path/'a.png';source.write_bytes(b'late-browser-file-read')
        record=FileRecord('test-session','fref_test','a.png','image/png',source.stat().st_size,
            hashlib.sha256(source.read_bytes()).hexdigest(),'unknown','caller_declared',time.time()+1000,source)
        browser=GeminiBrowser(replace(Settings.from_env(tmp_path,provider='gemini'),jobs_dir=tmp_path/'jobs'))
        browser._open_upload_menu=AsyncMock()
        uploaded=[]
        class Input:
            @property
            def first(self):return self
            async def set_input_files(self,value):uploaded.append(value)
        class Page:
            def locator(self,*_):return Input()
            async def evaluate(self,*_):
                return {'image_count':int(bool(uploaded)),'busy':False,'text':'a.png'}
        metadata=await browser._upload(Page(),[record])
        assert metadata[0]['verified']
        assert uploaded==[str(source)]
        # The browser may open this path only after the helper has returned.
        assert Path(uploaded[0]).read_bytes()==b'late-browser-file-read'
    from pathlib import Path
    asyncio.run(run())


@pytest.mark.parametrize('continuation', [False, True])
def test_prepared_receipt_is_available_before_click_failure(tmp_path, continuation):
    async def check(browser,page):
        browser._managed_page=AsyncMock(return_value=page)
        await page.get_by_role('button',name='Send message',exact=True).evaluate('e=>e.disabled=true')
        saved=[]
        async def save_receipt(receipt):
            assert await page.evaluate('sends')==0
            assert receipt.resolved_model=='4 Argon'
            assert receipt.resolved_reasoning_effort=='High'
            assert receipt.submitted_prompt_sha256==normalized_text_sha256('ambiguous click fixture')
            assert receipt.submission_state=='unconfirmed'
            assert receipt.selection_verified_at
            saved.append(receipt)
        call=browser.continue_conversation if continuation else browser.submit
        first='https://gemini.google.com/app/local-fixture' if continuation else 'consult'
        with submission_diagnostics(browser.settings.jobs_dir,'cgo_click_fixture','gemini'):
            with pytest.raises(Exception):
                await call(first,'ambiguous click fixture',model='Argon',quality='high',on_submission_prepared=save_receipt)
        assert len(saved)==1
        assert await page.evaluate('sends')==0
        assert await page.locator('.ql-editor').inner_text()=='ambiguous click fixture'
        diagnostic=json.loads((browser.settings.jobs_dir/'diagnostics/cgo_click_fixture.json').read_text(encoding='utf-8'))
        assert diagnostic['send_attempted'] is True
    run_gemini_page(tmp_path,gemini_picker(delay=10,thinking_delay=10),check,timeout=.8)


def test_prepared_receipt_persistence_failure_stops_before_click(tmp_path):
    async def check(browser,page):
        browser._managed_page=AsyncMock(return_value=page)
        async def reject_receipt(_receipt):
            raise OSError('isolated receipt store is unavailable')
        with submission_diagnostics(browser.settings.jobs_dir,'cgo_save_fixture','gemini'):
            with pytest.raises(OSError,match='receipt store'):
                await browser.submit('consult','must not send',model='Argon',quality='high',on_submission_prepared=reject_receipt)
        assert await page.evaluate('sends')==0
        diagnostic=json.loads((browser.settings.jobs_dir/'diagnostics/cgo_save_fixture.json').read_text(encoding='utf-8'))
        assert diagnostic['send_attempted'] is False
    run_gemini_page(tmp_path,gemini_picker(),check)
