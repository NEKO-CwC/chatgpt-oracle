from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest

from chatgpt_oracle.browser import BrowserCapacityError, BrowserPageNotReady, BrowserSnapshot, ModelUnavailableError, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job
from chatgpt_oracle.scheduler import SchedulerBusy


class Browser:
    def __init__(self, *, state='COMPLETE', failure=None):
        self.state, self.failure = state, failure
        self.pages = 0
        self.sends = 0
        self.events = []
        self.manager = None

    async def submit(self, *args, **kwargs):
        if isinstance(self.failure,BrowserCapacityError):
            raise self.failure
        self.pages = 1
        if self.failure:raise self.failure
        self.sends += 1
        return SubmitReceipt('https://chatgpt.com/c/test',0)

    async def inspect(self, receipt):
        return BrowserSnapshot(self.state,receipt.conversation_url,result_text='answer',user_action='human action' if self.state=='NEEDS_USER_INPUT' else None)

    async def close_owned_pages(self, *, keep_for_user=False, retain_key=None):
        assert self.manager.scheduler.status()['active_task_id'] is not None
        self.events.append(('release',keep_for_user,retain_key))
        if not keep_for_user:self.pages = 0

    async def close_retained_pages(self, key):
        self.pages = 0

    async def close_conversation_tabs(self, url):
        self.pages = 0
        return True

    async def capabilities(self):
        self.events.append(('probe',))
        return 'capabilities'

    async def close(self):
        self.pages = 0


def manager(tmp_path,browser):
    settings = replace(Settings.from_env(tmp_path),jobs_dir=tmp_path/'jobs',sessions_dir=tmp_path/'sessions',consult_poll_seconds=0.01,consult_timeout_seconds=1)
    value=JobManager(settings,browser)
    browser.manager=value
    return value


def reserve(manager,job):
    manager.store.save(job)
    manager.scheduler.enqueue(job.id,job.mode,on_busy='queue',retry_after_seconds=1)
    manager.scheduler.set_state(job.id,'QUEUED')
    assert manager.scheduler.reserve_next(os.getpid(),manager._owner_token)==job.id


@pytest.mark.parametrize('failure',[None,ModelUnavailableError('pro',['Extra High']),RuntimeError('send failed')])
def test_terminal_resource_cleanup_precedes_slot_release(tmp_path,failure):
    async def run():
        browser=Browser(failure=failure);m=manager(tmp_path,browser)
        job=Job.create('consult','test');reserve(m,job)
        await m._process(job)
        assert browser.pages==0
        assert browser.events==[('release',False,job.id)]
        assert m.scheduler.status()['active_task_id'] is None
        assert m.get(job.id).state==('COMPLETE' if failure is None else 'MODEL_UNAVAILABLE' if isinstance(failure,ModelUnavailableError) else 'ERROR')
        await m.stop()
    asyncio.run(run())


def test_human_action_releases_slot_without_resubmitting_and_can_be_cancelled(tmp_path):
    async def run():
        browser=Browser(state='NEEDS_USER_INPUT');m=manager(tmp_path,browser)
        job=Job.create('consult','test');reserve(m,job)
        await m._process(job)
        assert m.get(job.id).state=='NEEDS_USER_INPUT'
        assert browser.sends==1 and browser.pages==1
        assert m.scheduler.reserve_next(os.getpid(),m._owner_token) is None
        assert browser.events==[('release',True,job.id)]
        await m.cancel(job.id)
        assert m.get(job.id).state=='CANCELLED' and browser.pages==0
        assert browser.sends==1
        await m.stop()
    asyncio.run(run())


def test_tab_limit_keeps_task_queued_without_sending(tmp_path, monkeypatch):
    async def run():
        browser=Browser(failure=BrowserCapacityError('four tabs'));m=manager(tmp_path,browser)
        job=Job.create('consult','test');reserve(m,job)
        await m._process(job)
        assert m.get(job.id).state=='QUEUED' and browser.sends==0
        assert m.scheduler.status(job.id)['scheduler_state']=='QUEUED'
        browser.failure=None
        assert m.scheduler.reserve_next(os.getpid(),m._owner_token) is None
        with monkeypatch.context() as clock:
            import time
            after_backoff=time.time()+1
            clock.setattr('chatgpt_oracle.scheduler.time.time',lambda:after_backoff)
            assert m.scheduler.reserve_next(os.getpid(),m._owner_token)==job.id
        await m._process(m.get(job.id))
        assert m.get(job.id).state=='COMPLETE' and browser.sends==1 and browser.pages==0
        await m.stop()
    asyncio.run(run())


def test_unready_attachment_task_does_not_report_upload_in_progress(tmp_path):
    async def run():
        browser=Browser(failure=BrowserPageNotReady('picker unready'));m=manager(tmp_path,browser)
        upload=await m.uploads.put('new-test.zip','application/zip',b'PK-test')
        observed=[]
        original=browser.submit
        async def capture(*args,**kwargs):
            current=m.store.all()[0]
            observed.append((current.state,current.progress))
            return await original(*args,**kwargs)
        browser.submit=capture
        task=await m.submit('critic','test',quality='extra_high',upload_ids=[upload.upload_id])
        for _ in range(100):
            if m.get(task.id).state=='ERROR' and m.scheduler.status()['active_task_id'] is None:break
            await asyncio.sleep(0.01)
        result=m.get(task.id)
        assert result.error_code=='BROWSER_NOT_READY'
        assert result.progress is None and browser.sends==0
        assert observed[0][0]=='STARTING' and '附件尚未在网页验证' in observed[0][1]
        assert browser.pages==0 and m.scheduler.status()['active_task_id'] is None
        await m.stop()
    asyncio.run(run())


def test_busy_diagnostic_does_not_queue_or_close_active_job_pages(tmp_path):
    async def run():
        browser=Browser();m=manager(tmp_path,browser)
        assert m.scheduler.acquire_external_slot('active',os.getpid(),'external-owner')
        browser.pages=1
        with pytest.raises(SchedulerBusy) as exc:
            await asyncio.wait_for(m.browser_capabilities(),1)
        assert exc.value.info.reason=='BROWSER_DIAGNOSTIC_BUSY'
        assert m.scheduler.status()['queue_length']==0
        assert browser.pages==1 and browser.events==[]
        assert m.scheduler.status()['active_task_id']=='active'
        m.scheduler.release_external_slot('active','external-owner')
        assert await m.browser_capabilities()=='capabilities'
        assert browser.pages==0
        await m.stop()
    asyncio.run(run())


def test_session_close_cancels_its_queued_tasks(tmp_path):
    async def run():
        browser=Browser();m=manager(tmp_path,browser)
        assert m.scheduler.acquire_external_slot('active',os.getpid(),'external-owner')
        job=await m.submit('consult','queued',session_id='session-a')
        close=asyncio.create_task(m.session_close('session-a'))
        for _ in range(50):
            if m.get(job.id).state=='CANCELLED':break
            await asyncio.sleep(0.01)
        assert m.get(job.id).state=='CANCELLED'
        m.scheduler.release_external_slot('active','external-owner')
        result=await asyncio.wait_for(close,2)
        assert result['state']=='DEAD' and result['tab_open'] is False
        assert browser.sends==0
        await m.stop()
    asyncio.run(run())


def test_expired_session_retries_failed_cleanup(tmp_path):
    async def run():
        browser=Browser();m=manager(tmp_path,browser)
        session=m.sessions.create('session-a');session.conversation_url='https://chatgpt.com/c/test'
        session.expires_at='2000-01-01T00:00:00+00:00';m.sessions.save(session)
        original=browser.close_conversation_tabs
        async def fail(url):raise RuntimeError('page did not close')
        browser.close_conversation_tabs=fail
        with pytest.raises(RuntimeError):await m._expire_sessions()
        assert m.sessions.get('session-a').tab_cleanup_pending
        assert m.scheduler.status()['active_task_id'] is None
        browser.close_conversation_tabs=original
        await m._expire_sessions()
        assert not m.sessions.get('session-a').tab_cleanup_pending
        await m.stop()
    asyncio.run(run())


def test_cleanup_can_unblock_a_task_waiting_for_tab_capacity(tmp_path):
    async def run():
        browser=Browser(failure=BrowserCapacityError('full'));m=manager(tmp_path,browser)
        session=m.sessions.create('manual-session');session.conversation_url='https://chatgpt.com/c/manual'
        m.sessions.save(session)
        async def free(url):
            browser.failure=None
            browser.pages=0
            return True
        browser.close_conversation_tabs=free
        job=await m.submit('consult','queued')
        for _ in range(100):
            if m.get(job.id).error_code=='BROWSER_TAB_LIMIT':break
            await asyncio.sleep(0.01)
        assert browser.sends==0
        closed=await asyncio.wait_for(m.session_close('manual-session'),2)
        assert closed['state']=='DEAD'
        for _ in range(150):
            if m.get(job.id).state=='COMPLETE':break
            await asyncio.sleep(0.01)
        assert m.get(job.id).state=='COMPLETE' and browser.sends==1
        await m.stop()
    asyncio.run(run())
