"""Attachment tooltip regressions in an isolated Chrome with local DOM only."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from chatgpt_oracle.gemini_browser import GeminiTaskError
from chatgpt_oracle.uploads import InMemoryUpload, UploadError, UploadMetadata
from test_gemini_selection import run_gemini_page


def upload_dom(*, focus_tooltip=False, delayed_menu=0, drop_second=False):
    config = json.dumps(dict(focus_tooltip=focus_tooltip, delayed_menu=delayed_menu,
                             drop_second=drop_second))
    return r'''<!doctype html><meta charset="utf-8">
    <style>
      #upload{position:absolute;left:30px;top:60px;width:130px;height:40px}
      #uploads{position:absolute;left:30px;top:110px;background:white}
      #cards{position:absolute;left:300px;top:60px}
      .attachment{display:block;width:140px;height:75px;background:#ddd}
      .attachment img{width:30px;height:30px}
      .mat-mdc-tooltip-surface{position:absolute;left:20px;top:50px;width:180px;height:70px;
        background:#222;color:white;z-index:100;pointer-events:auto}
    </style>
    <input-container>
      <rich-textarea><div class="ql-editor" role="textbox" contenteditable="true">draft kept verbatim</div></rich-textarea>
      <button id="upload" aria-label="Upload & tools" aria-expanded="false" aria-controls="uploads">Upload</button>
      <div id="uploads" role="menu" style="display:none">
        <button role="menuitem">Upload files</button>
        <input id="image" type="file" accept="image/*" hidden>
      </div>
      <div id="cards"></div>
    </input-container>
    <button aria-label="Send message" onclick="window.sends++">Send</button>
    <div class="cdk-overlay-container"></div>
    <script>
    const config=CONFIG,button=document.querySelector('#upload'),menu=document.querySelector('#uploads');
    window.opens=0;window.badToggle=0;window.uploads=0;window.sends=0;window.tooltipShows=0;window.tooltipDismissals=0;
    button.onclick=()=>{
      if(button.getAttribute('aria-expanded')==='true'){
        window.badToggle++;button.setAttribute('aria-expanded','false');menu.style.display='none';return;
      }
      window.opens++;button.setAttribute('aria-expanded','true');
      setTimeout(()=>{menu.style.display='block';},config.delayed_menu);
    };
    function show(card,name){
      if(document.querySelector('.mat-mdc-tooltip-surface'))return;
      const tip=document.createElement('div');tip.className='mat-mdc-tooltip-surface mdc-tooltip__surface';
      tip.setAttribute('role','tooltip');tip.textContent=name;
      document.querySelector('.cdk-overlay-container').append(tip);window.tooltipShows++;
      const hide=()=>setTimeout(()=>{
        if(card.matches(':hover') || tip.matches(':hover') || (config.focus_tooltip && document.activeElement===card))return;
        if(tip.isConnected){tip.remove();window.tooltipDismissals++;}
      },80);
      card.addEventListener('mouseleave',hide);card.addEventListener('blur',hide);tip.addEventListener('mouseleave',hide);
    }
    document.querySelector('#image').onchange=event=>{
      window.uploads++;menu.style.display='none';button.setAttribute('aria-expanded','false');
      const file=event.target.files[0];
      if(config.drop_second && window.uploads===2)return;
      const card=document.createElement('div');card.className='attachment';card.tabIndex=0;
      const name=document.createElement('span');name.textContent=file.name;card.append(name);
      const img=document.createElement('img');img.alt=file.name;img.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';card.append(img);
      const remove=document.createElement('button');remove.setAttribute('aria-label','Remove file '+file.name);remove.textContent='x';
      remove.onclick=()=>card.remove();card.append(remove);
      const busy=document.createElement('span');busy.setAttribute('role','progressbar');card.append(busy);
      card.onmouseenter=()=>show(card,file.name);card.onfocus=()=>{if(config.focus_tooltip)show(card,file.name);};
      document.querySelector('#cards').append(card);setTimeout(()=>busy.remove(),300);
    };
    </script>'''.replace('CONFIG', config)


def image_upload(name):
    data = b'isolated-image-bytes-' + name.encode()
    return InMemoryUpload(UploadMetadata(name, name, 'image/png', len(data),
        hashlib.sha256(data).hexdigest(), 0, 9999999999), data)


def test_preview_tooltip_reproduces_pointer_interception_then_upload_recovers(tmp_path):
    async def check(browser, page):
        await browser._upload(page, [image_upload('first.png')])
        await page.locator('.attachment').hover()
        await page.locator('[role="tooltip"]').wait_for(state='visible')
        with pytest.raises(PlaywrightTimeoutError, match='intercepts pointer events'):
            await page.get_by_role('button', name='Upload & tools', exact=True).click(timeout=250)
        result = await browser._upload(page, [image_upload('second.png')])
        assert result[0]['verified'] is True
        assert await page.locator('.attachment').count() == 2
        assert await page.locator('.ql-editor').inner_text() == 'draft kept verbatim'
        assert await page.evaluate('({opens,badToggle,sends,tooltipShows,tooltipDismissals})') == dict(
            opens=2, badToggle=0, sends=0, tooltipShows=1, tooltipDismissals=1)
    run_gemini_page(tmp_path, upload_dom(), check)


@pytest.mark.parametrize('controlled', [True, False])
def test_multi_image_upload_moves_off_each_preview_before_reopening(tmp_path, controlled):
    async def check(browser, page):
        if not controlled:
            await page.locator('#upload').evaluate("e=>e.removeAttribute('aria-controls')")
        async def hover_first_preview():
            await page.locator('.attachment').first.wait_for(state='visible')
            await page.locator('.attachment').first.hover()
            await page.locator('[role="tooltip"]').wait_for(state='visible')
        hover = asyncio.create_task(hover_first_preview())
        try:
            result = await browser._upload(page, [image_upload('first.png'), image_upload('second.png')])
            await hover
        finally:
            if not hover.done():
                hover.cancel()
                await asyncio.gather(hover, return_exceptions=True)
        assert [item['filename'] for item in result] == ['first.png', 'second.png']
        assert all(item['verified'] for item in result)
        assert await page.locator('.attachment').count() == 2
        assert await page.evaluate('({opens,badToggle,sends,tooltipShows,tooltipDismissals})') == dict(
            opens=2, badToggle=0, sends=0, tooltipShows=1, tooltipDismissals=1)
        assert await page.locator('.ql-editor').inner_text() == 'draft kept verbatim'
    run_gemini_page(tmp_path, upload_dom(), check)


def test_upload_menu_already_open_is_reused_without_toggling(tmp_path):
    async def check(browser, page):
        await browser._open_upload_menu(page)
        await browser._open_upload_menu(page)
        await browser._upload(page, [image_upload('first.png')])
        assert await page.evaluate('({opens,badToggle,sends})') == dict(opens=1, badToggle=0, sends=0)
    run_gemini_page(tmp_path, upload_dom(), check)


def test_upload_menu_waits_for_expanded_trigger_to_hydrate(tmp_path):
    async def check(browser, page):
        await page.get_by_role('button', name='Upload & tools', exact=True).click()
        await browser._open_upload_menu(page)
        assert await page.get_by_role('menuitem', name='Upload files').is_visible()
        assert await page.evaluate('({opens,badToggle,sends})') == dict(opens=1, badToggle=0, sends=0)
    run_gemini_page(tmp_path, upload_dom(delayed_menu=250), check)


def test_uncontrolled_upload_trigger_does_not_accept_a_stale_model_menu(tmp_path):
    async def check(browser, page):
        await page.locator('#upload').evaluate("e=>e.removeAttribute('aria-controls')")
        await page.evaluate("""() => {
          const stale=document.createElement('div');stale.setAttribute('role','menu');
          stale.innerHTML='<div role="menuitem">4 Argon</div>';document.body.append(stale);
        }""")
        await browser._open_upload_menu(page)
        assert await page.get_by_role('menuitem', name='Upload files').is_visible()
        assert await page.evaluate('({opens,badToggle,sends})') == dict(opens=1, badToggle=0, sends=0)
    run_gemini_page(tmp_path, upload_dom(delayed_menu=250), check)


def test_persistent_focus_tooltip_uses_the_upload_buttons_native_keyboard_action(tmp_path):
    async def check(browser, page):
        await browser._upload(page, [image_upload('first.png')])
        await page.locator('.attachment').focus()
        await browser._upload(page, [image_upload('second.png')])
        assert await page.locator('.attachment').count() == 2
        assert await page.locator('.ql-editor').inner_text() == 'draft kept verbatim'
        assert await page.evaluate('({opens,badToggle,sends})') == dict(opens=2, badToggle=0, sends=0)
        assert await page.evaluate('tooltipShows') == 1
    run_gemini_page(tmp_path, upload_dom(focus_tooltip=True), check)


def test_keyboard_tooltip_recovery_does_not_bypass_a_modal(tmp_path):
    async def check(browser, page):
        await browser._upload(page, [image_upload('first.png')])
        await page.locator('.attachment').focus()
        await page.evaluate("""() => {
          const modal=document.createElement('div');modal.setAttribute('role','dialog');
          modal.textContent='Unrelated dialog';document.body.append(modal);
        }""")
        with pytest.raises(GeminiTaskError) as error:
            await browser._upload(page, [image_upload('second.png')])
        assert error.value.code == 'UPLOAD_MENU_BLOCKED'
        assert await page.locator('.attachment').count() == 1
        assert await page.locator('.ql-editor').inner_text() == 'draft kept verbatim'
        assert await page.evaluate('({opens,badToggle,sends})') == dict(opens=1, badToggle=0, sends=0)
    run_gemini_page(tmp_path, upload_dom(focus_tooltip=True), check)


def test_failed_second_attachment_preserves_first_attachment_and_draft(tmp_path):
    async def check(browser, page):
        with pytest.raises(UploadError, match='readiness was not verified'):
            await browser._upload(page, [image_upload('first.png'), image_upload('second.png')])
        assert await page.locator('.attachment').count() == 1
        assert await page.locator('.attachment').inner_text() == 'first.pngx'
        assert await page.locator('.ql-editor').inner_text() == 'draft kept verbatim'
        assert await page.evaluate('sends') == 0
    run_gemini_page(tmp_path, upload_dom(drop_second=True), check, timeout=.8)


def test_missing_menu_has_bounded_wait_without_retoggling_or_changing_draft(tmp_path):
    async def check(browser, page):
        await page.evaluate("() => {document.querySelector('#upload').onclick=()=>{window.opens++;document.querySelector('#upload').setAttribute('aria-expanded','true');};}")
        with pytest.raises(GeminiTaskError) as error:
            await browser._open_upload_menu(page)
        assert error.value.code == 'UPLOAD_MENU_UNAVAILABLE'
        assert await page.locator('.ql-editor').inner_text() == 'draft kept verbatim'
        assert await page.evaluate('({opens,badToggle,sends})') == dict(opens=1, badToggle=0, sends=0)
    run_gemini_page(tmp_path, upload_dom(), check, timeout=.8)
