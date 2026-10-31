from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace

import pytest

from chatgpt_oracle.browser import BrowserCapacityError, BrowserSnapshot, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.scheduler import IdempotencyConflict


class ConcurrentBrowser:
    def __init__(self):
        self.scope = ContextVar("session", default=None)
        self.active = {}
        self.releases = {}
        self.pages = {}
        self.sent = []
        self.attachments = []
        self.capacity_failures = 0
        self.manager = None
        self.sent_bodies = []

    @asynccontextmanager
    async def operation_scope(self, session_id=None, conversation_url=None):
        token = self.scope.set(session_id)
        try:
            yield
        finally:
            self.scope.reset(token)

    async def submit(self, mode, prompt, **kwargs):
        if self.capacity_failures:
            self.capacity_failures -= 1
            raise BrowserCapacityError("synthetic physical tab pressure before send")
        key = self.scope.get()
        return await self._send(key, f"https://chatgpt.com/c/{key}", prompt, kwargs)

    async def continue_conversation(self, url, prompt, **kwargs):
        key = self.scope.get()
        assert self.pages[key] == url
        return await self._send(key, url, prompt, kwargs)

    async def _send(self, key, url, prompt, kwargs):
        self.sent_bodies.append(prompt)
        # Coordination events are keyed by caller intent, while the browser
        # observes the independently persisted final Web body.
        persisted = [j for j in self.manager.store.all() if j.sending_text == prompt and j.session_id == key]
        assert persisted
        prompt = persisted[-1].prompt
        assert key not in self.active, "same Session operations overlapped"
        self.active[key] = prompt
        self.releases.setdefault(prompt, asyncio.Event())
        self.pages[key] = url
        self.sent.append((key, prompt))
        self.attachments.append([item.data for item in kwargs.get("attachments", [])])
        return SubmitReceipt(url, len(self.sent), resolved_reasoning_effort="Extra High")

    async def inspect(self, receipt):
        key = self.scope.get()
        prompt = self.active[key]
        if self.releases[prompt].is_set():
            self.active.pop(key)
            return BrowserSnapshot("COMPLETE", receipt.conversation_url, result_text="answer " + prompt)
        return BrowserSnapshot("RUNNING", receipt.conversation_url)

    async def close_owned_pages(self, keep_for_user=False, retain_key=None):
        if not keep_for_user:
            self.pages.pop(self.scope.get(), None)

    async def close_conversation_tabs(self, url):
        for key, value in list(self.pages.items()):
            if value == url:
                assert key not in self.active
                self.pages.pop(key)
        return True

    async def close_retained_pages(self, key):
        self.pages.pop(key, None)

    async def has_conversation_tab(self, url):
        return url in self.pages.values()

    async def close(self):
        pass


def make_manager(tmp_path, browser):
    manager = JobManager(replace(Settings.from_env(tmp_path), jobs_dir=tmp_path / "jobs",
        sessions_dir=tmp_path / "sessions", consult_poll_seconds=0.01,
        consult_timeout_seconds=10), browser)
    browser.manager = manager
    return manager


async def until(predicate, timeout=4):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


def test_four_sessions_generate_and_fifth_does_not_block_resident_followup(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        manager = make_manager(tmp_path, browser)
        try:
            jobs = [await manager.submit("consult", f"first-{i}", session_id=f"s{i}") for i in range(4)]
            await until(lambda: all(manager.get(job.id).state == "RUNNING" for job in jobs))
            assert manager.scheduler.status()["active_count"] == 4
            fifth = await manager.submit("consult", "fifth", session_id="s4")
            followup = await manager.resume_session("s0", "second-0")
            assert manager.get(fifth.id).state == manager.get(followup.id).state == "QUEUED"
            browser.releases["first-0"].set()
            await until(lambda: manager.get(followup.id).state == "RUNNING")
            assert manager.get(fifth.id).state == "QUEUED"
            assert (await manager.session_status("s0"))["tab_open"]
            browser.releases["second-0"].set()
            await until(lambda: manager.get(followup.id).state == "COMPLETE" and not manager.scheduler.session_active("s0"))
            assert len(browser.pages) == 4
            await manager.session_close("s0")
            await until(lambda: manager.get(fifth.id).state == "RUNNING")
            assert set(browser.pages) == {"s1", "s2", "s3", "s4"}
            for event in browser.releases.values():
                event.set()
            await until(lambda: manager.scheduler.status()["active_count"] == 0)
            for key in list(browser.pages):
                await manager.session_close(key)
            assert manager.scheduler.status()["session_slots"] == []
            assert browser.pages == {}
        finally:
            await manager.stop()
    asyncio.run(run())


def test_followups_before_first_receipt_keep_fifo_and_parent_identity(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        manager = make_manager(tmp_path, browser)
        try:
            first = await manager.submit("consult", "one", session_id="same")
            second = await manager.resume_session("same", "two")
            third = await manager.resume_session("same", "three")
            assert second.parent_id == first.id and third.parent_id == second.id
            assert manager.sessions.get("same").last_task_id == third.id
            for prompt, job in (("one", first), ("two", second), ("three", third)):
                await until(lambda: manager.get(job.id).state == "RUNNING")
                assert browser.active == {"same": prompt}
                assert manager.sessions.get("same").last_task_id == third.id
                browser.releases[prompt].set()
                await until(lambda: manager.get(job.id).state == "COMPLETE")
            assert browser.sent == [("same", "one"), ("same", "two"), ("same", "three")]
            assert len(browser.pages) == 1
        finally:
            await manager.stop()
    asyncio.run(run())


def test_capacity_requeue_retains_upload_until_terminal(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        browser.capacity_failures = 1
        manager = make_manager(tmp_path, browser)
        try:
            upload = await manager.uploads.put("new.zip", "application/zip", b"new zip content")
            job = await manager.submit("consult", "with-upload", session_id="upload-session", upload_ids=[upload.upload_id])
            await until(lambda: manager.get(job.id).state == "RUNNING")
            assert browser.attachments == [[b"new zip content"]]
            assert len(browser.sent) == 1
            browser.releases["with-upload"].set()
            await until(lambda: manager.get(job.id).state == "COMPLETE" and not manager.scheduler.session_active("upload-session"))
            assert upload.upload_id not in manager.uploads._entries
        finally:
            await manager.stop()
    asyncio.run(run())


def test_named_submit_idempotency_survives_bound_session(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        manager = make_manager(tmp_path, browser)
        try:
            job = await manager.submit("consult", "one", session_id="same", request_id="unique-first")
            await until(lambda: manager.get(job.id).state == "RUNNING")
            duplicate = await manager.submit("consult", "one", session_id="same", request_id="unique-first")
            assert duplicate.id == job.id and len(browser.sent) == 1
            with pytest.raises(IdempotencyConflict):
                await manager.submit("consult", "different", session_id="same", request_id="unique-first")
            browser.releases["one"].set()
            await until(lambda: manager.get(job.id).state == "COMPLETE")
        finally:
            await manager.stop()
    asyncio.run(run())


def test_expiry_observes_work_owned_by_another_manager(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        first = make_manager(tmp_path, browser)
        observer = make_manager(tmp_path, browser)
        try:
            job = await first.submit("consult", "working", session_id="keep-alive")
            await until(lambda: first.get(job.id).state == "RUNNING")
            session = first.sessions.get("keep-alive")
            session.expires_at = "2000-01-01T00:00:00+00:00"
            first.sessions.save(session)
            await observer._expire_sessions()
            assert first.sessions.get("keep-alive").state == "LIVE"
            assert "keep-alive" in browser.pages
            browser.releases["working"].set()
            await until(lambda: first.get(job.id).state == "COMPLETE" and not first.scheduler.session_active("keep-alive"))
            session = first.sessions.get("keep-alive")
            session.expires_at = "2000-01-01T00:00:00+00:00"
            first.sessions.save(session)
            await observer._expire_sessions()
            assert first.sessions.get("keep-alive").state == "DEAD"
            assert "keep-alive" not in browser.pages
            assert first.scheduler.status()["session_count"] == 0
        finally:
            await first.stop()
            await observer.stop()
    asyncio.run(run())


def test_url_export_routes_to_owning_named_session(tmp_path):
    manager = make_manager(tmp_path, ConcurrentBrowser())
    manager.sessions.touch("owner", conversation_url="https://chatgpt.com/c/existing")
    key, url = manager._resolve_export_session(task_id=None, session_id=None,
        conversation_url="https://chatgpt.com/c/existing")
    assert key == "owner" and url == "https://chatgpt.com/c/existing"
    assert len(manager.sessions.all()) == 1
    manager.exports.close()


def test_close_during_upload_admission_does_not_revive_session(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        manager = make_manager(tmp_path, browser)
        manager.sessions.create("closing")
        entered, release = asyncio.Event(), asyncio.Event()
        original = manager.uploads.attach
        async def delayed(ids, job_id):
            entered.set()
            await release.wait()
            return await original(ids, job_id)
        manager.uploads.attach = delayed
        upload = await manager.uploads.put("new.zip", "application/zip", b"bytes")
        submitting = asyncio.create_task(manager.submit("consult", "never-send", session_id="closing", upload_ids=[upload.upload_id]))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            closed = await asyncio.wait_for(manager.session_close("closing"), 3)
            assert closed["state"] == "DEAD"
            release.set()
            job = await submitting
            assert job.state == "CANCELLED"
            assert manager.sessions.get("closing").state == "DEAD"
            assert browser.sent == [] and upload.upload_id not in manager.uploads._entries
        finally:
            release.set()
            await asyncio.gather(submitting, return_exceptions=True)
            await manager.stop()
    asyncio.run(run())


def test_stop_and_restart_during_cancel_only_polls_existing_send(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        manager = make_manager(tmp_path, browser)
        job = await manager.submit("consult", "single-send", session_id="cancel-resume")
        await until(lambda: manager.get(job.id).state == "RUNNING")
        await manager.cancel(job.id)
        await manager.stop()
        assert manager.get(job.id).state == "CANCEL_REQUESTED"
        assert manager.scheduler.status(job.id)["scheduler_state"] == "QUEUED"
        restarted = make_manager(tmp_path, browser)
        try:
            await restarted.start()
            browser.releases["single-send"].set()
            await until(lambda: restarted.get(job.id).state == "CANCELLED")
            assert browser.sent == [("cancel-resume", "single-send")]
        finally:
            await restarted.stop()
    asyncio.run(run())


def test_close_fifth_unsent_session_does_not_wait_for_four_generators(tmp_path):
    async def run():
        browser = ConcurrentBrowser()
        manager = make_manager(tmp_path,browser)
        try:
            jobs = [await manager.submit('consult',f'active-{i}',session_id=f's{i}') for i in range(4)]
            await until(lambda: all(manager.get(j.id).state == 'RUNNING' for j in jobs))
            fifth = await manager.submit('consult','never-send',session_id='fifth')
            result = await asyncio.wait_for(manager.session_close('fifth'), .5)
            assert result['state'] == 'DEAD' and not result['tab_open']
            assert manager.get(fifth.id).state == 'CANCELLED'
            assert manager.scheduler.status()['active_count'] == 4
            assert ('fifth','never-send') not in browser.sent
            for event in browser.releases.values():
                event.set()
            await until(lambda: manager.scheduler.status()['active_count'] == 0)
        finally:
            await manager.stop()
    asyncio.run(run())
