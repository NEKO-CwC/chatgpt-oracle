import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx

from chatgpt_oracle.browser import ChatGPTBrowser
from chatgpt_oracle.config import Settings


def test_broker_restart_observes_a_retained_tab_without_starting_a_driver(tmp_path,monkeypatch):
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json=[{"type":"page","id":"retained","url":"https://chatgpt.com/c/retained"}])
    original=httpx.AsyncClient
    monkeypatch.setattr('chatgpt_oracle.browser.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    async def run():
        browser=ChatGPTBrowser(replace(Settings.from_env(tmp_path),browser_profile_dir=tmp_path/'profile',browser_cdp_url='http://127.0.0.1:9222'))
        browser.start=AsyncMock(side_effect=AssertionError('a status read must not start a browser'))
        assert await browser.has_conversation_tab('https://chatgpt.com/c/retained') is True
        assert browser._context is None and browser._playwright is None
        browser.start.assert_not_awaited()
    asyncio.run(run())
    assert len(requests)==1 and requests[0].method=='GET' and requests[0].url.path=='/json/list'
