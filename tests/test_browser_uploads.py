from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserTaskError
from chatgpt_oracle.file_store import FileRecord
from chatgpt_oracle.uploads import InMemoryUpload, UploadMetadata
from test_browser_controls import picker, run_page


def stored_zip(tmp_path: Path) -> FileRecord:
    path = tmp_path / "probe.zip"
    path.write_bytes(b"PK\x03\x04fixture")
    return FileRecord("test", "fref_fixture", path.name, "application/zip", path.stat().st_size,
                      "fixture-hash", "original", "upload", 9999999999, path)


@pytest.mark.parametrize("stored", [False, True])
@pytest.mark.parametrize("remove_label", ["移除 ", "Remove file "])
def test_zip_upload_uses_generic_input_and_current_visible_hover_controls(tmp_path, stored, remove_label):
    body = picker() + '''
      <input type="file" id="generic" multiple aria-label="附加文件">
      <input type="file" id="image-only" accept="image/*" multiple onchange="window.wrongInput=true">
      <div hidden><button aria-label="REMOVE_LABELprobe.zip">hidden duplicate</button></div>
      <style>.file-card .remove {opacity:0;pointer-events:none}.file-card:hover .remove {opacity:1;pointer-events:auto}</style>
      <div id="cards"></div>
      <script>
      document.querySelector('#generic').onchange=event=>{
        for(const file of event.target.files){
          const card=document.createElement('div'); card.className='file-card';
          card.style='position:relative;width:180px;height:80px';
          const name=document.createElement('span');name.textContent=file.name;card.append(name);
          const remove=document.createElement('button');remove.className='remove';
          remove.setAttribute('aria-label','REMOVE_LABEL'+file.name);remove.textContent='x';
          remove.onclick=()=>card.remove();card.append(remove);document.querySelector('#cards').append(card);
        }
      };
      </script>
    '''.replace("REMOVE_LABEL", remove_label)

    async def check(browser, page):
        record = stored_zip(tmp_path)
        upload = record if stored else InMemoryUpload(
            UploadMetadata("legacy", record.filename, record.mime_type, record.size,
                           record.sha256, 0, 9999999999), record.path.read_bytes(),
        )
        receipts = await browser._upload_files(page, [upload])
        assert receipts[0]["filename"] == "probe.zip"
        assert receipts[0]["verified"] is True
        assert await page.evaluate("window.wrongInput === true") is False
        assert await page.locator('#generic').evaluate("e=>e.files[0].name") == "probe.zip"
        await browser._clear_uploaded_files(page, [record.filename])
        assert await page.locator('#cards .file-card').count() == 0
        assert await page.evaluate("window.sent") == 0
    run_page(tmp_path, body, check)


@pytest.mark.parametrize("method", ["submit", "continue"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_file_ref_failure_preserves_original_error_and_never_sends(tmp_path, method, cleanup_fails):
    async def check(browser, page):
        record = stored_zip(tmp_path)
        browser._upload_files = AsyncMock(side_effect=BrowserTaskError("original upload failure"))
        browser._clear_uploaded_files = AsyncMock(
            side_effect=RuntimeError("secondary cleanup failure") if cleanup_fails else None,
        )
        browser._click_send = AsyncMock()
        with pytest.raises(BrowserTaskError, match="original upload failure"):
            if method == "submit":
                await browser.submit("consult", "do not send", quality="extra_high", attachments=[record])
            else:
                url = "https://chatgpt.com/c/upload-regression"
                await page.goto(url)
                await browser.continue_conversation(url, "do not send", quality="extra_high", attachments=[record])
        browser._clear_uploaded_files.assert_awaited_once_with(page, [record.filename])
        browser._click_send.assert_not_awaited()
        assert await page.evaluate("window.sent") == 0
    run_page(tmp_path, picker(), check)


def test_hidden_picker_menus_do_not_block_selection(tmp_path):
    hidden = '<div role="menu" hidden><span role="slider" aria-valuemin="0" aria-valuemax="4" aria-valuenow="0"></span></div>'
    async def check(browser, page):
        effort, value, source = await browser._select_effort(page, "extra_high")
        assert (effort, value, source) == ("Extra High", 3, "explicit")
        assert await page.evaluate("window.sent") == 0
    run_page(tmp_path, hidden + picker() + hidden, check)
