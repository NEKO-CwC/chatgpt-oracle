from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, ChatGPTBrowser, SubmitReceipt
from chatgpt_oracle.browser_resources import ProfileResources, ResourceLockTimeout, UIAdmissionDeferred
from chatgpt_oracle.config import Settings
from chatgpt_oracle.gemini_browser import GeminiBrowser
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from chatgpt_oracle.submission_diagnostics import submission_phase


def settings(tmp_path, *, timeout=2):
    return replace(Settings.from_env(tmp_path),jobs_dir=tmp_path/'jobs',
        sessions_dir=tmp_path/'sessions',browser_profile_dir=tmp_path/'profile',
        browser_timeout_seconds=.05,consult_poll_seconds=.01,consult_timeout_seconds=timeout)


async def until(check, timeout=4):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(.01)


@pytest.mark.parametrize('provider',['chatgpt','gemini'])
def test_admission_defers_before_page_creation(provider,tmp_path):
    async def run():
        value=replace(settings(tmp_path),provider=provider)
        browser=ChatGPTBrowser(value) if provider=='chatgpt' else GeminiBrowser(value)
        browser._fresh_conversation_page=AsyncMock()
        browser._managed_page=AsyncMock()
        held,release=asyncio.Event(),asyncio.Event()
        async def owner():
            async with ProfileResources(value.browser_profile_dir).ui_locked():
                held.set()
                await release.wait()
        task=asyncio.create_task(owner())
        await held.wait()
        try:
            with pytest.raises(UIAdmissionDeferred):
                await browser.submit('consult','no send',dedicated_page=True)
            browser._fresh_conversation_page.assert_not_awaited()
            browser._managed_page.assert_not_awaited()
        finally:
            release.set();await task
    asyncio.run(run())


def test_same_task_can_nest_connector_resources_but_child_cannot_inherit_ownership(tmp_path):
    async def run():
        first=ProfileResources(tmp_path/'profile',lock_timeout_seconds=.03)
        connector=ProfileResources(tmp_path/'profile',lock_timeout_seconds=.03)
        async with first.ui_locked():
            async with connector.ui_locked():
                async def child():
                    async with connector.ui_admitted():
                        raise AssertionError('child inherited the lock')
                with pytest.raises(UIAdmissionDeferred):
                    await asyncio.create_task(child())
        async with connector.ui_admitted():
            pass
    asyncio.run(run())


class AdmissionBrowser:
    requires_turn_receipts=True
    def __init__(self, value, *, after_send_failure=False):
        self.resources=ProfileResources(value.browser_profile_dir,lock_timeout_seconds=.05)
        self.sent=[]
        self.after_send_failure=after_send_failure
    async def submit(self,mode,prompt,**kwargs):
        async with self.resources.ui_admitted():
            submission_phase('send_attempted')
            self.sent.append(prompt)
            if self.after_send_failure:
                raise ResourceLockTimeout('post-send lock timeout')
            return SubmitReceipt('https://chatgpt.com/c/fixture',0,
                submitted_prompt_sha256=normalized_text_sha256(prompt),current_user_message_id='u1',
                submission_state='confirmed',submission_verified_at='2026-10-16T00:00:00Z')
    async def inspect(self,receipt):
        return BrowserSnapshot('COMPLETE',receipt.conversation_url,result_text='answer')
    async def close(self):
        pass


@pytest.mark.parametrize('cancel',[False,True])
def test_busy_task_keeps_identity_upload_and_attempt_count_then_sends_once_or_cancels(tmp_path,cancel):
    async def run():
        value=settings(tmp_path)
        browser=AdmissionBrowser(value)
        manager=JobManager(value,browser)
        held,release=asyncio.Event(),asyncio.Event()
        async def owner():
            async with ProfileResources(value.browser_profile_dir).ui_locked():
                held.set();await release.wait()
        owner_task=asyncio.create_task(owner())
        await held.wait()
        try:
            upload=await manager.uploads.put('input.txt','text/plain',b'preserve attachment')
            job=await manager.submit('consult','question',session_id='s1',request_id='r1',upload_ids=[upload.upload_id])
            await until(lambda: manager.get(job.id).error_code=='BROWSER_UI_BUSY')
            queued=manager.get(job.id)
            assert queued.state=='QUEUED' and queued.attempts==0 and not browser.sent
            assert (await manager.uploads.take_for_job(job.id,[upload.upload_id]))[0].data==b'preserve attachment'
            frozen=queued.sending_text_sha256
            duplicate=await manager.submit('consult','question',session_id='s1',request_id='r1',upload_ids=[upload.upload_id])
            assert duplicate.id==job.id
            if cancel:
                await manager.cancel(job.id)
            release.set();await owner_task
            await until(lambda:manager.get(job.id).state==('CANCELLED' if cancel else 'COMPLETE'))
            await until(lambda:not manager.scheduler.session_active('s1'))
            assert len(browser.sent)==(0 if cancel else 1)
            assert manager.get(job.id).sending_text_sha256==frozen
            assert manager.get(job.id).attempts==(0 if cancel else 1)
            diagnostic=json.loads((value.jobs_dir/'diagnostics'/f'{job.id}.json').read_text(encoding='utf-8'))
            assert any(e['phase']=='ui_deferred' and e['lock_kind']=='ui' for e in diagnostic['events'])
            assert diagnostic['send_attempted'] is (not cancel)
        finally:
            release.set();await owner_task;await manager.stop()
    asyncio.run(run())


def test_admission_deadline_is_bounded_and_does_not_send(tmp_path):
    async def run():
        value=settings(tmp_path,timeout=.01)
        browser=AdmissionBrowser(value);manager=JobManager(value,browser)
        held,release=asyncio.Event(),asyncio.Event()
        async def owner():
            async with ProfileResources(value.browser_profile_dir).ui_locked():
                held.set();await release.wait()
        task=asyncio.create_task(owner());await held.wait()
        try:
            job=await manager.submit('consult','question')
            await until(lambda:manager.get(job.id).state=='ERROR')
            assert manager.get(job.id).error_code=='BROWSER_UI_WAIT_TIMEOUT'
            assert manager.get(job.id).attempts==0 and not browser.sent
        finally:
            release.set();await task;await manager.stop()
    asyncio.run(run())


def test_post_send_lock_timeout_never_requeues(tmp_path):
    async def run():
        value=settings(tmp_path);browser=AdmissionBrowser(value,after_send_failure=True)
        manager=JobManager(value,browser)
        try:
            job=await manager.submit('consult','question')
            await until(lambda:manager.get(job.id).state=='ERROR')
            assert manager.get(job.id).error_code=='BROWSER_LOCK_TIMEOUT'
            assert manager.get(job.id).attempts==1 and len(browser.sent)==1
            assert manager.scheduler.status()['queue_length']==0
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize('continuation',[False,True])
def test_chatgpt_url_and_identity_confirmation_do_not_hold_shared_ui_lock(tmp_path,continuation):
    async def run():
        value=replace(settings(tmp_path),browser_timeout_seconds=1)
        browser=ChatGPTBrowser(value)
        class Page:
            url='https://chatgpt.com'
            bring_to_front=AsyncMock()
        page=Page()
        composer=SimpleNamespace(fill=AsyncMock())
        browser._fresh_conversation_page=AsyncMock(return_value=page)
        browser._conversation_page=AsyncMock(return_value=page)
        browser._prepare_submission_page=AsyncMock(return_value=None)
        # Admission/acknowledgement timing is independent of editor internals.
        browser._fill_prepared_draft=AsyncMock()
        browser._require_composer=AsyncMock()
        browser._require_conversation_route=AsyncMock()
        browser._check_composer_draft=AsyncMock(return_value=composer)
        browser._apply_selection=AsyncMock(return_value=dict(resolved_model='fixture',resolved_reasoning_effort='High',selection_source='fixture',selection_verified_at='now'))
        browser._verify_selection_after_upload=AsyncMock()
        browser._upload_files=AsyncMock(return_value=[])
        browser._read_turn_surface=AsyncMock(return_value={'messages':[]})
        browser._click_send=AsyncMock()
        waiting,release=asyncio.Event(),asyncio.Event()
        async def wait_url(_):
            waiting.set();await release.wait()
            page.url='https://chatgpt.com/c/fixture'
        browser._wait_for_conversation_url=wait_url
        browser._confirm_submission=AsyncMock()
        if continuation:
            task=asyncio.create_task(browser.continue_conversation('https://chatgpt.com/c/fixture','question'))
        else:
            task=asyncio.create_task(browser.submit('consult','question',dedicated_page=True))
        try:
            await asyncio.wait_for(waiting.wait(),2)
            async with ProfileResources(value.browser_profile_dir,lock_timeout_seconds=.05).ui_admitted():
                browser._click_send.assert_awaited_once()
                browser._verify_selection_after_upload.assert_awaited_once()
            release.set();await task
        finally:
            release.set();await task
    asyncio.run(run())
