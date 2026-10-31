from unittest.mock import AsyncMock
import asyncio
from dataclasses import replace

import pytest

from chatgpt_oracle.browser import BrowserNeedsUserAction, BrowserPageNotReady, BrowserAccountPageUnavailable
from chatgpt_oracle.browser import ChatGPTBrowser
from chatgpt_oracle.config import Settings
from test_oracle_browser_boundaries import run_dom, URL


def test_account_failure_with_a_fallback_composer_is_not_ready_without_claiming_logout(tmp_path):
    async def check(browser, page):
        with pytest.raises(BrowserAccountPageUnavailable) as caught:
            await browser._require_composer(page)
        assert caught.value.code == "ACCOUNT_PAGE_UNAVAILABLE"
        assert caught.value.user_input_kind == "page_recovery"
        status = await browser.browser_status()
        assert status["ready"] is False and status["error_code"] == "ACCOUNT_PAGE_UNAVAILABLE"
    run_dom(tmp_path, '<main><h1>无法加载你的账户</h1><button>重新加载</button><button>退出登录</button><div contenteditable="true"></div></main>', check, url="https://chatgpt.com/")


def test_redirected_continuation_never_fills_or_sends_on_home(tmp_path):
    async def check(browser, page):
        browser._apply_selection = AsyncMock()
        browser._click_send = AsyncMock()
        with pytest.raises(BrowserPageNotReady):
            await browser.continue_conversation(URL, "do not send on wrong page", quality="extra_high")
        browser._apply_selection.assert_not_awaited()
        browser._click_send.assert_not_awaited()
        assert await page.locator('[contenteditable]').inner_text() == ""
    run_dom(tmp_path, '<main><div contenteditable="true"></div></main>', check, url="https://chatgpt.com/")


def test_account_error_quote_inside_a_conversation_is_not_an_account_blocker(tmp_path):
    async def check(browser, page):
        await browser._require_composer(page)
        await browser._require_conversation_route(page, URL)
    run_dom(tmp_path, '<main><div data-message-author-role="assistant">无法加载你的账户</div><div contenteditable="true"></div></main>', check)


def test_account_error_never_refreshes_outside_fresh_submission_preparation(tmp_path):
    async def run():
        browser=ChatGPTBrowser(replace(Settings.from_env(tmp_path),browser_profile_dir=tmp_path/'profile'))
        class Page:
            reload=AsyncMock()
            goto=AsyncMock()
        page=Page()
        browser._owned_pages[page]='owned-target'
        browser._check_composer_draft=AsyncMock()
        browser._require_composer_once=AsyncMock(side_effect=BrowserAccountPageUnavailable('transient'))
        with pytest.raises(BrowserAccountPageUnavailable):
            await browser._require_composer(page,conversation_url=URL)
        page.goto.assert_not_awaited()
        page.reload.assert_not_awaited()
        browser._require_composer_once.assert_awaited_once()
    asyncio.run(run())


def test_existing_page_logout_requires_manual_login_without_reloading(tmp_path):
    async def run():
        browser=ChatGPTBrowser(replace(Settings.from_env(tmp_path),browser_profile_dir=tmp_path/'profile'))
        class Page:
            reload=AsyncMock()
        page=Page()
        browser._owned_pages[page]='owned-target'
        browser._check_composer_draft=AsyncMock()
        browser._require_composer_once=AsyncMock(side_effect=BrowserNeedsUserAction('login_required','human login'))
        with pytest.raises(BrowserNeedsUserAction) as caught:
            await browser._require_composer(page)
        assert caught.value.reason=='login_required' and page.reload.await_count==0
    asyncio.run(run())
