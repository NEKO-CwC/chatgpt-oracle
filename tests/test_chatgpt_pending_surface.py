from __future__ import annotations

import asyncio
from dataclasses import replace
from html import escape
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, SubmitReceipt
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import TERMINAL_STATES, utc_now
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from chatgpt_oracle.scheduler import BusyInfo, SchedulerBusy
from test_jobs import settings
from test_oracle_browser_boundaries import run_dom, URL


PROMPT = "The original pending question."


def current_receipt():
    return SubmitReceipt(URL, 0, current_user_message_id="current-user",
        submitted_prompt_sha256=normalized_text_sha256(PROMPT),
        submission_state="confirmed", submission_verified_at=utc_now())


def body(*, state="in_progress", answer=False, previous=""):
    final = '<div data-markdown-text-style="assistant-message">The final answer.</div>' if answer else ""
    return previous + f'''<main><div data-talvt-turn-state="{state}">
      <div data-chatgpt-search-unit-key="current-user:user">{escape(PROMPT)}</div>
      <div data-chatgpt-search-unit-key="current-answer:assistant">
        <div data-markdown-text-style="assistant-message" data-markdown-text-tone="tertiary">Private thinking summary, never an answer.</div>{final}
      </div></div><div data-map-composer-conversation="local-chatgpt:pending"><textarea></textarea></div></main>'''


def test_in_progress_without_stop_button_is_observed_without_returning_thinking(tmp_path):
    async def check(browser, page):
        surface = await browser._read_turn_surface(page)
        assert surface["generation_active"] is True
        assert surface["generation_state"] == "in_progress"
        assert surface["provisional_conversation"] is True
        assert [(m["id"], m["role"]) for m in surface["messages"]] == [("current-user", "user")]
        result = await browser.inspect(current_receipt())
        assert result.state == "RUNNING" and result.generation_active is True
        assert result.result_text is None and result.current_assistant_message_id is None
        assert await browser._active_ui(page)
    run_dom(tmp_path, body(), check)


def test_new_search_key_reads_final_output_without_thinking_summary(tmp_path):
    async def check(browser, page):
        receipt = current_receipt()
        result = await browser.inspect(receipt)
        assert result.state == "COMPLETE" and result.generation_active is False
        assert result.result_text == "The final answer."
        assert receipt.current_assistant_message_id == "current-answer"
    run_dom(tmp_path, body(state="completed", answer=True), check)


def test_an_old_in_progress_marker_does_not_hide_the_latest_completed_turn(tmp_path):
    async def check(browser, page):
        assert (await browser._read_turn_surface(page))["generation_active"] is False
        assert (await browser.inspect(current_receipt())).state == "COMPLETE"
    run_dom(tmp_path, body(state="completed", answer=True,
        previous='<div data-talvt-turn-state="in_progress">old virtualized turn</div>'), check)


@pytest.mark.parametrize("active", [False, True])
def test_observation_timeout_reports_last_ui_state_without_stopping_or_resending(tmp_path, active):
    class PendingBrowser:
        requires_turn_receipts = True
        sends = 0
        stop = AsyncMock(side_effect=AssertionError("Timeout must not stop upstream"))

        async def submit(self, mode, prompt, **kwargs):
            self.sends += 1
            receipt = current_receipt()
            receipt.submitted_prompt_sha256 = normalized_text_sha256(prompt)
            return receipt

        async def inspect(self, receipt):
            return BrowserSnapshot("RUNNING", URL, generation_active=active)

        async def close(self):
            pass

    async def run():
        browser = PendingBrowser()
        manager = JobManager(replace(settings(tmp_path), consult_timeout_seconds=.08), browser)
        try:
            job = await manager.submit("consult", PROMPT, request_id="one-pending-question", session_id="pending-session")
            async with asyncio.timeout(2):
                while manager.get(job.id).state not in TERMINAL_STATES:
                    await asyncio.sleep(.01)
            saved = manager.get(job.id)
            assert saved.state == "TIMEOUT" and saved.submission_state == "confirmed"
            assert saved.error_code == ("GENERATION_WAIT_TIMEOUT" if active else "TURN_RESULT_WAIT_TIMEOUT")
            assert saved.current_user_message_id == "current-user" and saved.conversation_url == URL
            assert browser.sends == 1
            browser.stop.assert_not_awaited()
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("tool", ["browser_status", "browser_capabilities"])
def test_mcp_browser_diagnostics_expose_structured_retryable_busy(monkeypatch, tmp_path, tool):
    monkeypatch.setenv("CHATGPT_ORACLE_ROOT", str(tmp_path))
    monkeypatch.setenv("CHATGPT_ORACLE_TRANSPORT", "stdio")
    monkeypatch.setenv("CHATGPT_PROFILE_DIR", str(tmp_path / "profile"))
    import chatgpt_oracle.server as server
    monkeypatch.setattr(server.manager, "start", AsyncMock())
    monkeypatch.setattr(server.manager, tool, AsyncMock(side_effect=SchedulerBusy(
        BusyInfo("original-task", 0, 5, "BROWSER_DIAGNOSTIC_BUSY"))))
    handler = server.chatgpt_browser_status if tool == "browser_status" else server.chatgpt_capabilities
    result = asyncio.run(handler())
    assert result["state"] == "BUSY" and result["retryable"] is True
    assert result["error"]["code"] == "BROWSER_DIAGNOSTIC_BUSY"
