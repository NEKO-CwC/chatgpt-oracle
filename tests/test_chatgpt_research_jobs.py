from __future__ import annotations

import asyncio
from dataclasses import replace

from chatgpt_oracle.browser import BrowserSnapshot, _prompt_text_sha256
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job, TERMINAL_STATES
from test_jobs import FakeBrowser, settings


URL = "https://chatgpt.com/c/research-workflow"


class ResearchBrowser(FakeBrowser):
    requires_turn_receipts = True

    def __init__(self, snapshots, *, hang_start=False):
        super().__init__(snapshots)
        self.manager = None
        self.start_calls = 0
        self.stop_calls = 0
        self.start_entered = asyncio.Event()
        self.start_cancelled = asyncio.Event()
        self.hang_start = hang_start

    async def submit(self, mode, prompt, *, dedicated_page=False, on_submission_prepared=None, **kwargs):
        receipt = await super().submit(mode, prompt, **kwargs)
        receipt.conversation_url = URL
        receipt.current_user_message_id = "research-user"
        receipt.current_assistant_message_id = "research-assistant"
        receipt.submitted_prompt_sha256 = _prompt_text_sha256(prompt)
        receipt.submission_state = "confirmed"
        receipt.research_expected = mode == "research"
        if on_submission_prepared:
            await on_submission_prepared(receipt)
        return receipt

    async def start_research(self, receipt):
        self.start_calls += 1
        stored = self.manager.store.all()[0]
        assert stored.research_start_state == "requested"
        assert receipt.research_start_state == "requested"
        self.start_entered.set()
        if self.hang_start:
            try:
                await asyncio.Event().wait()
            finally:
                self.start_cancelled.set()
        return True

    async def request_stop(self, receipt):
        self.stop_calls += 1
        return True


def observed(state="RUNNING", text=None, *, complete=False, plan=False):
    return BrowserSnapshot(
        state, URL, result_text=text, research_observed=True,
        research_state="report_ready" if complete else "plan_ready" if plan else "running",
        research_plan_ready=plan, research_report_complete=complete,
        research_run_id="matched-run", research_report_id="matched-report" if complete else None,
        generation_active=not complete,
    )


async def settled(manager, task_id):
    async with asyncio.timeout(3):
        while manager.get(task_id).state not in TERMINAL_STATES or manager.scheduler.status()["active_count"]:
            await asyncio.sleep(0.005)
    return manager.get(task_id)


def test_consult_triggered_research_cannot_publish_stable_progress(tmp_path):
    async def run():
        browser = ResearchBrowser([
            observed("COMPLETE", "Researching 4 sources"),
            observed("COMPLETE", "Researching 4 sources"),
            observed("COMPLETE", "Actual report", complete=True),
            observed("COMPLETE", "Actual report", complete=True),
        ])
        manager = JobManager(settings(tmp_path), browser)
        browser.manager = manager
        try:
            job = await manager.submit("consult", "one research invocation", request_id="unique-consult")
            done = await settled(manager, job.id)
            assert done.state == "COMPLETE"
            assert done.research_observed and done.research_expected
            assert done.research_report_complete
            assert done.research_run_id == "matched-run"
            assert done.research_report_id == "matched-report"
            _, result = manager.result(job.id)
            assert "Actual report" in result and "Researching" not in result
            assert len(browser.submitted) == 1
            assert browser.start_calls == 0
        finally:
            await manager.stop()
    asyncio.run(run())


def test_stable_placeholder_times_out_without_result(tmp_path):
    async def run():
        browser = ResearchBrowser([observed("COMPLETE", "Your report will appear here")] * 20)
        cfg = replace(settings(tmp_path), research_timeout_seconds=0.08)
        manager = JobManager(cfg, browser)
        browser.manager = manager
        try:
            job = await manager.submit("research", "wait for a report")
            done = await settled(manager, job.id)
            assert done.state == "TIMEOUT"
            assert done.error_code == "RESEARCH_REPORT_WAIT_TIMEOUT"
            assert not done.result_path and not done.research_report_complete
            assert len(browser.submitted) == 1
        finally:
            await manager.stop()
    asyncio.run(run())


def test_observed_research_uses_one_bounded_research_budget_in_consult(tmp_path):
    async def run():
        browser = ResearchBrowser([
            *[observed() for _ in range(6)],
            observed("COMPLETE", "Delayed real report", complete=True),
            observed("COMPLETE", "Delayed real report", complete=True),
        ])
        cfg = replace(settings(tmp_path), consult_timeout_seconds=0.02,
                      research_timeout_seconds=0.3, research_poll_seconds=0.01)
        manager = JobManager(cfg, browser)
        browser.manager = manager
        try:
            job = await manager.submit("consult", "research tool is selected by the page")
            done = await settled(manager, job.id)
            assert done.state == "COMPLETE"
            assert done.mode == "consult" and done.research_observed
            assert len(browser.submitted) == 1
        finally:
            await manager.stop()
    asyncio.run(run())


def test_plan_start_intent_is_saved_before_one_action(tmp_path):
    async def run():
        browser = ResearchBrowser([
            observed("RESEARCH_PLAN_READY", plan=True), observed(),
            observed("COMPLETE", "Finished report", complete=True),
            observed("COMPLETE", "Finished report", complete=True),
        ])
        manager = JobManager(settings(tmp_path), browser)
        browser.manager = manager
        try:
            job = await manager.submit("research", "start once", request_id="start-once")
            done = await settled(manager, job.id)
            assert done.state == "COMPLETE"
            assert done.research_start_state == "confirmed"
            assert browser.start_calls == 1 and len(browser.submitted) == 1
        finally:
            await manager.stop()
    asyncio.run(run())


def test_recovered_pending_start_never_repeats_start_or_prompt(tmp_path):
    async def run():
        browser = ResearchBrowser([observed("RESEARCH_PLAN_READY", plan=True)])
        manager = JobManager(settings(tmp_path), browser)
        browser.manager = manager
        job = Job.create("research", "already submitted")
        job.state = "RUNNING"
        job.conversation_url = URL
        job.submission_state = "confirmed"
        job.current_user_message_id = "research-user"
        job.current_assistant_message_id = "research-assistant"
        job.submitted_prompt_sha256 = _prompt_text_sha256(job.prompt)
        job.research_expected = True
        job.research_start_state = "requested"
        job.research_run_id = "matched-run"
        manager.store.save(job)
        try:
            await manager._process(job)
            done = manager.get(job.id)
            assert done.state == "ERROR"
            assert done.error_code == "RESEARCH_START_UNCONFIRMED"
            assert done.completed_at
            assert done.research_start_state == "requested"
            assert browser.start_calls == 0 and browser.submitted == []
        finally:
            await manager.stop()
    asyncio.run(run())


def test_cancel_interrupts_plan_start_and_releases_lane(tmp_path):
    async def run():
        browser = ResearchBrowser([observed("RESEARCH_PLAN_READY", plan=True)], hang_start=True)
        manager = JobManager(replace(settings(tmp_path), browser_timeout_seconds=20), browser)
        browser.manager = manager
        try:
            job = await manager.submit("research", "cancel this research", session_id="research-owner")
            await asyncio.wait_for(browser.start_entered.wait(), timeout=2)
            await manager.cancel(job.id)
            done = await settled(manager, job.id)
            assert done.state == "CANCELLED"
            assert browser.start_cancelled.is_set()
            assert done.research_start_state == "requested"
            assert done.cancellation_stop_verified is True
            assert browser.start_calls == 1 and browser.stop_calls == 1
            assert len(browser.submitted) == 1
        finally:
            await manager.stop()
    asyncio.run(run())
