from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

import chatgpt_oracle.browser as browser_module
from chatgpt_oracle.browser import BrowserTaskError, ChatGPTBrowser
from chatgpt_oracle.config import Settings


class ChunkedBytes(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        for offset in range(0, len(self.content), 65536):
            yield self.content[offset : offset + 65536]


def test_signed_download_streams_bounded_blocks_and_validates_redirects(monkeypatch, tmp_path: Path) -> None:
    content = bytes(range(251)) * 4097
    requests: list[str] = []

    async def response(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert "authorization" not in request.headers
        if request.url.path == "/malicious":
            return httpx.Response(302, headers={"Location": "https://evil.example/file"})
        if request.url.path == "/initial":
            assert request.headers.get("cookie") == "session=private-cookie"
            return httpx.Response(302, headers={"Location": "/content"})
        if request.url.path == "/cross-origin":
            assert request.headers.get("cookie") == "session=private-cookie"
            return httpx.Response(302, headers={"Location": "https://files.oaiusercontent.com/cross-content"})
        if request.url.path == "/cross-content":
            assert "cookie" not in request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "image/png",
                "Content-Disposition": 'attachment; filename="diagram.png"',
            },
            stream=ChunkedBytes(content),
        )

    actual_client = httpx.AsyncClient

    def mock_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return actual_client(transport=httpx.MockTransport(response), *args, **kwargs)

    monkeypatch.setattr(browser_module.httpx, "AsyncClient", mock_client)
    browser = ChatGPTBrowser(Settings.from_env(tmp_path))

    class BrowserCookieContext:
        async def cookies(self, url: str) -> list[dict[str, str]]:
            if httpx.URL(url).host == "chatgpt.com":
                return [{"name": "session", "value": "private-cookie", "domain": ".chatgpt.com", "path": "/"}]
            return []

    browser._context = BrowserCookieContext()
    destination = tmp_path / "staging" / "diagram.png"
    destination.parent.mkdir()
    progress: list[int] = []

    async def run() -> None:
        async def report(value: int) -> None:
            progress.append(value)

        result = await browser._stream_content_to_path(
            "https://chatgpt.com/initial",
            destination,
            max_file_bytes=len(content),
            on_progress=report,
        )
        assert result.size == len(content)
        assert result.mime_type == "image/png"
        assert destination.stat().st_size == len(content)
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == hashlib.sha256(content).hexdigest()

        cross_origin = await browser._stream_content_to_path(
            "https://chatgpt.com/cross-origin",
            destination,
            max_file_bytes=len(content),
        )
        assert cross_origin.size == len(content)

        with pytest.raises(BrowserTaskError, match="verified download hosts"):
            await browser._stream_content_to_path(
                "https://files.oaiusercontent.com/malicious",
                destination,
                max_file_bytes=len(content),
            )

    asyncio.run(run())
    assert requests == [
        "https://chatgpt.com/initial",
        "https://chatgpt.com/content",
        "https://chatgpt.com/cross-origin",
        "https://files.oaiusercontent.com/cross-content",
        "https://files.oaiusercontent.com/malicious",
    ]
    assert progress[-1] == len(content)
