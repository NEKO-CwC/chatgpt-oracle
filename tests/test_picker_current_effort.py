import asyncio
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserTaskError, ChatGPTBrowser
from test_browser_controls import picker, run_page


def test_verified_current_extra_high_does_not_scan_other_efforts(tmp_path):
    async def check(browser, page):
        await page.locator('#slider').evaluate("node => node.setAttribute('aria-valuenow','3')")
        await page.locator('#label').evaluate("node => node.textContent='Extra High'")
        await page.locator('#trigger').evaluate("node => node.textContent='Extra High'")
        browser._discover_efforts = AsyncMock(side_effect=AssertionError("unnecessary range scan"))
        assert await browser._select_effort(page, "extra_high") == ("Extra High", 3, "explicit")
        browser._discover_efforts.assert_not_awaited()
        assert await page.evaluate('window.sent') == 0
        assert await page.locator('#slider').get_attribute('aria-valuenow') == '3'
    run_page(tmp_path, picker(), check)


@pytest.mark.parametrize('actual,expected', [
    ('High', True), (' high ', True), ('Extra High', False), ('Extra_High', False),
    ('High priority', False), ('Higher', False),
])
def test_high_is_a_discrete_effort_label(actual, expected):
    assert ChatGPTBrowser._effort_matches('High', actual) is expected


def test_high_does_not_reuse_current_extra_high(tmp_path):
    async def check(browser, page):
        await page.evaluate('''() => {
          document.querySelector('#slider').setAttribute('aria-valuenow','3');
          document.querySelector('#label').textContent='Extra High';
          document.querySelector('#trigger').textContent='Extra High';
        }''')
        assert await browser._select_effort(page, 'High') == ('High', 2, 'explicit')
        assert await page.locator('#slider').get_attribute('aria-valuenow') == '2'
        assert await page.evaluate('window.sent') == 0
    run_page(tmp_path, picker(), check)


@pytest.mark.parametrize('model', ['Latest', 'GPT-5.6 Sol'])
def test_effort_animation_overlay_cannot_block_model_selection(tmp_path, model):
    body = picker() + '''<script>
      window.keyboardSelections=0;window.pointerSelections=0;
      document.querySelector('#toggle').addEventListener('click',()=>{
        const radios=[...document.querySelectorAll('[role=menuitemradio]')];
        const first=radios[0].getBoundingClientRect(),last=radios.at(-1).getBoundingClientRect();
        let overlay=document.querySelector('#effortOverlay');
        if(!overlay){
          overlay=document.createElement('div');overlay.id='effortOverlay';
          overlay.dataset.active='true';overlay.setAttribute('aria-hidden','false');
          overlay.innerHTML='<span data-effort-only="true">Extra High</span>';
          document.querySelector('#menu').append(overlay);
        }
        overlay.style=`position:fixed;left:${first.left}px;top:${first.top}px;width:${first.width}px;height:${last.bottom-first.top}px;z-index:9999`;
      });
      document.querySelectorAll('[role=menuitemradio]').forEach(r=>{
        r.addEventListener('keydown',e=>{if(e.key==='Enter')window.keyboardSelections++});
        r.addEventListener('click',e=>{if(e.detail)window.pointerSelections++});
      });
    </script>'''
    async def check(browser, page):
        await page.locator('[contenteditable=true]').fill('preserved draft')
        page.set_default_timeout(1500)
        assert await browser._select_model_option(page, model) == model
        assert await page.locator('[contenteditable=true]').inner_text() == 'preserved draft'
        assert await page.evaluate('window.pointerSelections') == 0
        assert await page.evaluate('window.keyboardSelections') == (0 if model == 'Latest' else 1)
        assert await page.evaluate('window.sent') == 0
    run_page(tmp_path, body, check)


@pytest.mark.parametrize('continuation', [False, True])
def test_final_effort_verification_catches_drift_without_attachments(tmp_path, continuation):
    body = picker() + '''<script>
      document.querySelector('[contenteditable=true]').addEventListener('input',()=>{
        document.querySelector('#slider').setAttribute('aria-valuenow','3');
        document.querySelector('#label').textContent='Extra High';
        document.querySelector('#trigger').textContent='Extra High';
      });
    </script>'''
    async def check(browser, page):
        browser._click_send=AsyncMock()
        if continuation:
            await page.evaluate("history.replaceState({}, '', '/c/current')")
        action=(browser._prepare_continuation(page.url,'question',reasoning_effort='High') if continuation
                else browser._prepare_submit('critic','question',reasoning_effort='High'))
        with pytest.raises(BrowserTaskError, match='发送前档位验证失败：请求 High，页面显示 Extra High'):
            await action
        browser._click_send.assert_not_awaited()
        assert not browser._scope.send_attempted
        # A failed verification clears only the proven owned unsent draft.
        assert not (await page.locator('[contenteditable=true]').inner_text()).strip()
        assert await page.evaluate('window.sent') == 0
    run_page(tmp_path, body, check)


@pytest.mark.parametrize('allow_downgrade', [False, True])
def test_final_verifier_checks_requested_effort_instead_of_trusting_resolved(tmp_path, allow_downgrade):
    async def check(browser, page):
        await page.evaluate('''() => {
          document.querySelector('#slider').setAttribute('aria-valuenow','3');
          document.querySelector('#label').textContent='Extra High';
        }''')
        selection={'requested_reasoning_effort':'High', 'allow_downgrade':allow_downgrade,
                   'resolved_reasoning_effort':'Extra High', 'resolved_model':None}
        with pytest.raises(BrowserTaskError, match='请求 High，页面显示 Extra High'):
            await browser._verify_selection_after_upload(page,selection)
        assert 'selection_verified_at' not in selection
        assert await page.evaluate('window.sent') == 0
    run_page(tmp_path, picker(), check)


def test_final_verifier_preserves_explicit_pro_downgrade_contract(tmp_path):
    async def check(browser, page):
        selection=await browser._apply_selection(page,quality='pro',model=None,
                                                 reasoning_effort=None,allow_downgrade=True)
        assert selection['resolved_reasoning_effort'] == 'Extra High'
        assert selection['selection_source'] == 'downgrade'
        await browser._verify_selection_after_upload(page,selection)
        assert selection['resolved_reasoning_effort'] == 'Extra High'
        assert await page.evaluate('window.sent') == 0
    run_page(tmp_path, picker(pro=False), check)
