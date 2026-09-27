from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from chatgpt_oracle.browser import (
    ChatGPTBrowser,
    _attachment_scan_reaches_api_count,
    default_chrome_executable,
)
from chatgpt_oracle.config import Settings
from chatgpt_oracle.file_store import FileRecord
from chatgpt_oracle.uploads import InMemoryUpload, UploadMetadata


def test_upload_files_pass_exact_bytes_through_filechooser_and_input(tmp_path: Path) -> None:
    executable = default_chrome_executable()
    if executable is None or not executable.exists():
        pytest.skip("a local Chrome or Edge executable is required")

    data = b"\x00client payload\xff"
    metadata = UploadMetadata(
        upload_id="upl_browser_test",
        filename="notes.bin",
        mime_type="application/octet-stream",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at=1,
        expires_at=60,
    )
    upload = InMemoryUpload(metadata, data)
    stored_path = tmp_path / metadata.filename
    stored_path.write_bytes(data)
    stored_upload = FileRecord(
        session_id="agent-a",
        file_ref="fref_browser_test",
        filename=metadata.filename,
        mime_type=metadata.mime_type,
        size=metadata.size,
        sha256=metadata.sha256,
        representation="unknown",
        provenance="caller_declared",
        expires_at=metadata.expires_at,
        path=stored_path,
    )

    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=str(executable),
            )
            try:
                for chooser_path in (True, False):
                    for input_file in (upload, stored_upload):
                        page = await browser.new_page()
                        await page.set_content(
                            """
                            <button data-testid="composer-plus-btn" id="plus">+</button>
                            <input id="files" type="file" multiple hidden>
                            <script>
                              const input = document.querySelector('#files');
                              document.querySelector('#plus').addEventListener('click', () => {
                                const action = document.createElement('button');
                                action.setAttribute('role', 'menuitem');
                                action.textContent = 'Add files';
                                action.addEventListener('click', () => input.click());
                                document.body.appendChild(action);
                              });
                              input.addEventListener('change', () => {
                                for (const file of input.files) {
                                  const button = document.createElement('button');
                                  button.textContent = `Remove file ${file.name}`;
                                  document.body.appendChild(button);
                                }
                              });
                            </script>
                            """
                            if chooser_path
                            else """
                            <input id="files" type="file" multiple>
                            <script>
                              const input = document.querySelector('#files');
                              input.addEventListener('change', () => {
                                for (const file of input.files) {
                                  const button = document.createElement('button');
                                  button.textContent = `Remove file ${file.name}`;
                                  document.body.appendChild(button);
                                }
                              });
                            </script>
                            """
                        )

                        receipt = await ChatGPTBrowser(Settings.from_env(tmp_path))._upload_files(page, [input_file])
                        observed = await page.locator("#files").evaluate(
                            """async input => Promise.all(Array.from(input.files, async file => ({
                              name: file.name,
                              type: file.type,
                              bytes: Array.from(new Uint8Array(await file.arrayBuffer()))
                            })))"""
                        )
                        assert observed == [{
                            "name": "notes.bin",
                            "type": "application/octet-stream",
                            "bytes": list(data),
                        }]
                        assert receipt[0]["sha256"] == metadata.sha256
                        assert receipt[0]["verified"] is True
                        assert receipt[0]["upload_method"] == ("filechooser" if chooser_path else "input")
                        await page.close()
            finally:
                await browser.close()

    asyncio.run(run())


def test_virtualized_attachment_scan_visits_each_message_id(tmp_path: Path) -> None:
    executable = default_chrome_executable()
    if executable is None or not executable.exists():
        pytest.skip("a local Chrome or Edge executable is required")

    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=str(executable),
            )
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <div id="thread" style="height: 240px; overflow-y: auto; position: relative">
                      <div id="spacer" style="height: 7560px; position: relative"></div>
                    </div>
                    <script>
                      const thread = document.querySelector('#thread');
                      const spacer = document.querySelector('#spacer');
                      const rowHeight = 120;
                      function render() {
                        const first = Math.max(0, Math.floor(thread.scrollTop / rowHeight) - 1);
                        const count = Math.ceil(thread.clientHeight / rowHeight) + 2;
                        spacer.replaceChildren(...Array.from({length: count}, (_, offset) => {
                          const index = Math.min(62, first + offset);
                          const row = document.createElement('article');
                          row.dataset.messageId = `message-${index}`;
                          row.dataset.messageAuthorRole = 'assistant';
                          row.style.cssText = `position:absolute;top:${index * rowHeight}px;height:${rowHeight}px`;
                          return row;
                        }));
                      }
                      thread.addEventListener('scroll', render);
                      render();
                    </script>
                    """
                )

                scan = await ChatGPTBrowser(Settings.from_env(tmp_path))._load_all_rendered_messages(page)
                assert scan["stable"] is True
                assert scan["reached_end"] is True
                assert scan["rendered_message_count"] < 63
                assert scan["observed_message_id_count"] == 63
                assert len(scan["observed_message_ids"]) == 63
                assert set(scan["observed_message_scroll_tops"]) == set(scan["observed_message_ids"])
                assert _attachment_scan_reaches_api_count(scan, 63) is True
                await page.close()
            finally:
                await browser.close()

    asyncio.run(run())


def test_attachment_scan_stops_when_scroller_makes_no_progress(tmp_path: Path) -> None:
    class StuckPage:
        async def evaluate(self, *_: object) -> dict[str, object]:
            return {
                "scroller_count": 1,
                "original_scroll_top": 0,
                "scroll_top": 0,
                "max_scroll": 10000,
                "client_height": 600,
                "rendered_message_count": 1,
                "message_ids": ["message-1"],
            }

    async def run() -> None:
        scan = await ChatGPTBrowser(Settings.from_env(tmp_path))._load_all_rendered_messages(StuckPage())
        assert scan["stable"] is False
        assert scan["stop_reason"] == "no_scroll_progress"
        assert scan["iterations"] <= 26

    asyncio.run(run())
