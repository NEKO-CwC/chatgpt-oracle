"""Unbound sends are observed on their original page, never clicked again."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from chatgpt_oracle.browser import SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.gemini_browser import GeminiTaskError
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from chatgpt_oracle.submission_diagnostics import submission_diagnostics, submission_phase
from test_gemini_selection import gemini_picker, run_gemini_page
from test_gemini_turn_recovery import owned, rendered


PROMPT = "original private pending question"
HOME = "https://gemini.google.com/u/1/app?pli=1"


async def pending(browser, page):
    target = await owned(browser, page)
    await page.evaluate("history.replaceState(null,'','/u/1/app?pli=1')")
    browser._operation.account_index = 1
    return target, SubmitReceipt(HOME, 0, account_index=1,
        resolved_model="4 Argon", resolved_reasoning_effort="High",
        submitted_prompt_sha256=normalized_text_sha256(PROMPT), submission_state="unconfirmed")


def test_late_account_prefixed_url_confirms_original_turn_without_another_click(tmp_path):
    async def check(browser, page):
        target, receipt = await pending(browser, page)
        first = await browser.inspect_submission(receipt)
        assert first.state == "SEND_PENDING" and first.conversation_url is None
        await page.evaluate("history.replaceState(null,'','/u/1/app/late-ack?pli=1')")
        await page.evaluate("html=>document.body.insertAdjacentHTML('beforeend',html)", rendered([
            {"role": "user", "text": PROMPT},
            {"role": "assistant", "id": "late-answer", "text": "original answer"},
        ]))
        result = await browser.inspect_submission(receipt)
        assert result.state == "COMPLETE" and result.result_text == "original answer"
        assert receipt.submission_state == "confirmed"
        assert receipt.conversation_url == "https://gemini.google.com/u/1/app/late-ack"
        assert receipt.current_assistant_message_id == "late-answer"
        assert await browser._target_id(page) == target
        assert len(page.context.pages) == 1 and await page.evaluate("sends") == 0
    run_gemini_page(tmp_path, gemini_picker(), check, timeout=.3)


@pytest.mark.parametrize("draft", ["", "human draft"])
def test_unbound_acknowledgement_has_its_own_budget_and_preserves_page_and_draft(tmp_path, draft):
    async def check(browser, page):
        target, receipt = await pending(browser, page)
        await page.locator(".ql-editor").fill(draft)
        browser._operation.acknowledgement_started = asyncio.get_running_loop().time() - 61
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        with submission_diagnostics(browser.settings.jobs_dir, "cgo_ack_budget", "gemini"):
            with pytest.raises(GeminiTaskError) as error:
                await browser.inspect_submission(receipt)
        assert error.value.code == "SEND_UNCONFIRMED"
        assert "unbound" in str(error.value)
        assert receipt.submission_state == "unconfirmed" and receipt.current_user_message_id is None
        assert await page.locator(".ql-editor").inner_text() == draft
        assert await browser._target_id(page) == target and page.url == HOME
        assert requests == [] and await page.evaluate("sends") == 0
        saved = (browser.settings.jobs_dir / "diagnostics/cgo_ack_budget.json").read_text(encoding="utf-8")
        assert PROMPT not in saved and "human draft" not in saved
        event = json.loads(saved)["milestones"]["gemini_send_ack_unavailable"]
        assert event["composer_length"] == len(draft)
        assert event["path"] == "/u/1/app" and event["user_nodes"] == 0
    run_gemini_page(tmp_path, gemini_picker(), check, timeout=.3)


def test_user_rendered_before_url_is_observed_but_does_not_fabricate_confirmation(tmp_path):
    async def check(browser, page):
        _, receipt = await pending(browser, page)
        await page.evaluate("html=>document.body.insertAdjacentHTML('beforeend',html)", rendered([
            {"role": "user", "text": PROMPT},
        ]))
        browser.settings = replace(browser.settings, browser_timeout_seconds=.05)
        result = await browser._confirm_send(page, receipt)
        assert result.submission_state == "unconfirmed"
        assert result.current_user_message_id is not None
        assert browser._formal_url(result.conversation_url) is None
        assert await page.evaluate("sends") == 0
    run_gemini_page(tmp_path, gemini_picker(), check, timeout=.3)


def test_manual_account_change_while_pending_does_not_rebind_or_navigate(tmp_path):
    async def check(browser, page):
        _, receipt = await pending(browser, page)
        await page.evaluate("history.replaceState(null,'','/u/0/app?pli=1')")
        with pytest.raises(GeminiTaskError) as error:
            await browser.inspect_submission(receipt)
        assert error.value.code == "ACCOUNT_ROUTE_MISMATCH"
        assert page.url.endswith("/u/0/app?pli=1")
        assert await page.evaluate("sends") == 0
    run_gemini_page(tmp_path, gemini_picker(), check, timeout=.3)


def test_send_milestones_survive_repeated_pending_reads_without_prompt_content(tmp_path):
    with submission_diagnostics(tmp_path, "cgo_long_pending", "gemini"):
        submission_phase("gemini_before_send", composer_length=99, path="/u/1/app")
        submission_phase("send_attempted")
        for _ in range(100):
            submission_phase("gemini_turn_identity_missing", messages=[])
    record = json.loads((tmp_path / "diagnostics/cgo_long_pending.json").read_text(encoding="utf-8"))
    assert record["send_attempted"] is True
    assert len(record["events"]) == 48
    assert set(record["milestones"]) == {"gemini_before_send", "send_attempted"}


def test_broker_returns_uncertain_delivery_without_losing_receipt_or_replaying(tmp_path):
    class PendingGemini:
        requires_turn_receipts = True

        def __init__(self):
            self.sends = 0
            self.closed = []

        async def submit(self, mode, prompt, *, on_submission_prepared=None, **kwargs):
            receipt = SubmitReceipt(HOME, 0, submission_state="unconfirmed",
                submitted_prompt_sha256=normalized_text_sha256(prompt))
            await on_submission_prepared(receipt)
            self.sends += 1
            return receipt

        async def inspect_submission(self, receipt):
            # A last observed partial identity still belongs in the durable
            # failure receipt, even though its URL cannot be bound.
            receipt.current_user_message_id = "partial-original-user"
            raise GeminiTaskError("SEND_UNCONFIRMED", "Delivery uncertain; original page retained; Session is unbound.")

        async def close_owned_pages(self, **kwargs):
            self.closed.append(kwargs)

        async def close(self):
            pass

    async def check():
        settings = replace(Settings.from_env(tmp_path, provider="gemini"),
            jobs_dir=tmp_path / "jobs", sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile", shared_browser=False,
            consult_poll_seconds=.01, consult_timeout_seconds=3)
        browser = PendingGemini()
        manager = JobManager(settings, browser)
        try:
            job = await manager.submit("consult", PROMPT, session_id="uncertain-session", request_id="one-original-request")
            async with asyncio.timeout(3):
                while manager.get(job.id).state not in {"ERROR", "TIMEOUT"} or manager.scheduler.session_active("uncertain-session"):
                    await asyncio.sleep(.01)
            saved = manager.get(job.id)
            assert saved.state == "ERROR" and saved.error_code == "SEND_UNCONFIRMED"
            assert saved.submission_state == "unconfirmed" and saved.conversation_url is None
            assert saved.current_user_message_id == "partial-original-user"
            assert saved.request_id == "one-original-request" and browser.sends == 1
            assert manager.sessions.get("uncertain-session").conversation_url is None
            assert browser.closed[-1]["keep_for_user"] is True
        finally:
            await manager.stop()
    asyncio.run(check())
