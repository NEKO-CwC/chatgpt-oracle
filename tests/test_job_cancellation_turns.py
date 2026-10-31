from __future__ import annotations

import asyncio
from dataclasses import replace

from chatgpt_oracle.browser import BrowserSnapshot
from chatgpt_oracle.jobs import JobManager
from test_jobs import FakeBrowser, settings


class GenerationBrowser(FakeBrowser):
    def __init__(self, *, hang_inspect=False, stop_verified=True):
        super().__init__([])
        self.inspected = asyncio.Event()
        self.inspect_cancelled = asyncio.Event()
        self.hang_inspect = hang_inspect
        self.stop_verified = stop_verified
        self.stop_calls = 0
        self.closed_urls = []

    async def submit(self, *args, dedicated_page=False, **kwargs):
        receipt = await super().submit(*args, **kwargs)
        receipt.user_message_ids_before = ["old-user"]
        receipt.assistant_message_ids_before = ["old-assistant"]
        receipt.submitted_prompt_sha256 = "normalized-prompt-hash"
        receipt.current_user_message_id = "new-user"
        receipt.submission_state = "confirmed"
        receipt.submission_verified_at = "2026-10-04T00:00:00+00:00"
        return receipt

    async def inspect(self, receipt):
        self.inspected.set()
        if self.hang_inspect:
            try:
                await asyncio.Event().wait()
            finally:
                self.inspect_cancelled.set()
        return BrowserSnapshot("RUNNING", receipt.conversation_url, generation_active=True)

    async def request_stop(self, receipt):
        self.stop_calls += 1
        return self.stop_verified

    async def close_conversation_tabs(self, url):
        self.closed_urls.append(url)

    async def close_retained_pages(self, key):
        return None


async def wait_cancelled(manager, task_id):
    async def done():
        while manager.get(task_id).state != "CANCELLED" or manager.scheduler.status()["active_count"]:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(done(), timeout=0.7)


def test_cancel_wakes_a_long_poll_sleep_and_preserves_send_identity(tmp_path):
    async def run():
        browser = GenerationBrowser()
        cfg = replace(settings(tmp_path), consult_poll_seconds=60, consult_timeout_seconds=120)
        manager = JobManager(cfg, browser)
        try:
            job = await manager.submit("consult", "send exactly once")
            await asyncio.wait_for(browser.inspected.wait(), timeout=0.5)
            await manager.cancel(job.id)
            await wait_cancelled(manager, job.id)
            finished = manager.get(job.id)
            assert browser.submitted == [manager.get(job.id).sending_text]
            assert browser.stop_calls == 1
            assert finished.cancellation_stop_verified is True
            assert finished.cancel_requested_at is not None
            assert finished.submission_state == "confirmed"
            assert finished.current_user_message_id == "new-user"
            assert finished.assistant_message_ids_before == ["old-assistant"]
            assert not finished.result_path
        finally:
            await manager.stop()
    asyncio.run(run())


def test_cancel_interrupts_an_inflight_read_instead_of_waiting_its_deadline(tmp_path):
    async def run():
        browser = GenerationBrowser(hang_inspect=True)
        cfg = replace(settings(tmp_path), browser_timeout_seconds=60)
        manager = JobManager(cfg, browser)
        try:
            job = await manager.submit("consult", "single send")
            await asyncio.wait_for(browser.inspected.wait(), timeout=0.5)
            await manager.cancel(job.id)
            await wait_cancelled(manager, job.id)
            assert browser.inspect_cancelled.is_set()
            assert browser.stop_calls == 1
            assert browser.submitted == [manager.get(job.id).sending_text]
        finally:
            await manager.stop()
    asyncio.run(run())


def test_unverified_web_stop_closes_its_session_and_reports_the_gap(tmp_path):
    async def run():
        browser = GenerationBrowser(stop_verified=False)
        manager = JobManager(settings(tmp_path), browser)
        try:
            job = await manager.submit("consult", "cancel me", session_id="cancel-owner")
            await asyncio.wait_for(browser.inspected.wait(), timeout=0.5)
            await manager.cancel(job.id)
            await wait_cancelled(manager, job.id)
            finished = manager.get(job.id)
            assert finished.error_code == "CANCEL_STOP_UNCONFIRMED"
            assert finished.cancellation_stop_verified is False
            assert manager.sessions.get("cancel-owner").state == "DEAD"
            assert browser.closed_urls == ["https://chatgpt.com/c/test"]
            assert not manager.scheduler.status()["session_slots"]
            assert browser.submitted == [manager.get(job.id).sending_text]
        finally:
            await manager.stop()
    asyncio.run(run())


def test_close_metadata_only_export_session_does_not_join_foreign_conversation_lane(tmp_path):
    async def run():
        browser = GenerationBrowser()
        cfg = replace(settings(tmp_path), consult_poll_seconds=60)
        manager = JobManager(cfg, browser)
        try:
            job = await manager.submit("consult", "keep running", session_id="actual-owner")
            await asyncio.wait_for(browser.inspected.wait(), timeout=0.5)
            manager.sessions.create("diagnostic-reader")
            manager.sessions.touch("diagnostic-reader", conversation_url="https://chatgpt.com/c/test")
            closed = await asyncio.wait_for(manager.session_close("diagnostic-reader"), timeout=0.2)
            assert closed["state"] == "DEAD"
            assert manager.get(job.id).state == "RUNNING"
            assert browser.closed_urls == []
            await manager.cancel(job.id)
            await wait_cancelled(manager, job.id)
            await manager.session_close("actual-owner")
        finally:
            await manager.stop()
    asyncio.run(run())


def test_unconfirmed_send_releases_operation_without_publishing_old_answer(tmp_path):
    async def run():
        class UnconfirmedBrowser(GenerationBrowser):
            async def submit(self, *args, **kwargs):
                receipt = await super().submit(*args, **kwargs)
                receipt.submission_state = "unconfirmed"
                receipt.current_user_message_id = None
                return receipt
        browser = UnconfirmedBrowser()
        manager = JobManager(settings(tmp_path), browser)
        try:
            job = await manager.submit("consult", "ambiguous send")
            async def terminal():
                while manager.get(job.id).state != "ERROR" or manager.scheduler.status()["active_count"]:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(terminal(), timeout=0.5)
            finished = manager.get(job.id)
            assert finished.error_code == "SEND_UNCONFIRMED"
            assert finished.submission_state == "unconfirmed"
            assert not finished.result_path
            assert not browser.inspected.is_set()
            assert browser.submitted == [manager.get(job.id).sending_text]
        finally:
            await manager.stop()
    asyncio.run(run())


def test_dead_alias_session_status_does_not_report_a_foreign_tab(tmp_path):
    async def run():
        browser = GenerationBrowser()
        checked = []
        async def foreign_tab(url):
            checked.append(url)
            return True
        browser.has_conversation_tab = foreign_tab
        manager = JobManager(settings(tmp_path), browser)
        try:
            manager.sessions.create("closed-alias")
            manager.sessions.touch("closed-alias", conversation_url="https://chatgpt.com/c/test")
            manager.sessions.close("closed-alias")
            status = await manager.session_status("closed-alias")
            assert status["state"] == "DEAD" and status["tab_open"] is False
            assert checked == []
        finally:
            await manager.stop()
    asyncio.run(run())
