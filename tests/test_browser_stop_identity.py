from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

from chatgpt_oracle.browser import (
    BrowserSnapshot, ChatGPTBrowser, SubmitReceipt,
    _prompt_text_sha256, _resolve_current_turn,
)
from test_jobs import settings


def test_recycled_virtual_user_id_cannot_rebind_to_an_old_question():
    receipt = SubmitReceipt("https://chatgpt.com/c/test", 5)
    receipt.current_user_message_id = "fallback-turn-5:0"
    receipt.submitted_prompt_sha256 = _prompt_text_sha256("current question")
    receipt.submission_state = "confirmed"
    messages = [
        {"role": "user", "id": "fallback-turn-5:0", "text": "old different question"},
        {"role": "assistant", "id": "unseen-historical-answer", "text": "old reply"},
    ]
    assert _resolve_current_turn(receipt, messages) == []
    assert receipt.current_assistant_message_id is None


def test_stop_targets_confirmed_latest_turn_and_clicks_only_once(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(settings(tmp_path), browser_timeout_seconds=0.2))
        receipt = SubmitReceipt("https://chatgpt.com/c/test", 5)
        receipt.current_user_message_id = "user-current"
        receipt.submitted_prompt_sha256 = _prompt_text_sha256("current question")
        receipt.submission_state = "confirmed"
        clicks = []

        class Control:
            async def count(self): return 1
            def nth(self, index): return self
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def click(self, **kwargs): clicks.append("current-stop")

        class Page:
            frames = [None]
            def get_by_role(self, *args, **kwargs): return Control()
            async def wait_for_timeout(self, ms): await asyncio.sleep(0)
        page = Page()
        page.frames = [page]

        async def conversation(*args, **kwargs): return page
        async def inspect(current):
            return BrowserSnapshot("RUNNING", current.conversation_url, generation_active=not clicks)
        async def surface(current):
            return {"messages": [{"role": "user", "id": "user-current", "text": "current question"}]}
        async def composer(current): return True
        async def login(current): return False
        @asynccontextmanager
        async def transaction(current): yield
        browser._conversation_page = conversation
        browser.inspect = inspect
        browser._read_turn_surface = surface
        browser._page_has_composer = composer
        browser._page_requires_login = login
        browser._ui_transaction = transaction
        assert await browser.request_stop(receipt) is True
        assert clicks == ["current-stop"]
    asyncio.run(run())


def test_stop_does_not_click_a_later_human_turn_or_claim_unknown_send_stopped(tmp_path):
    async def run():
        browser = ChatGPTBrowser(settings(tmp_path))
        page = object()
        async def conversation(*args, **kwargs): return page
        async def inspect(current):
            return BrowserSnapshot("RUNNING", current.conversation_url, generation_active=True)
        async def surface(current):
            return {"messages": [{"role": "user", "id": "another-user", "text": "human's later question"}]}
        browser._conversation_page = conversation
        browser.inspect = inspect
        browser._read_turn_surface = surface
        receipt = SubmitReceipt("https://chatgpt.com/c/test", 5)
        assert await browser.request_stop(receipt) is False
        receipt.submission_state = "confirmed"
        receipt.current_user_message_id = "original-user"
        receipt.submitted_prompt_sha256 = _prompt_text_sha256("original question")
        assert await browser.request_stop(receipt) is False
    asyncio.run(run())
