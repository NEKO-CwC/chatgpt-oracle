from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.config import Settings
from chatgpt_oracle.browser import BrowserCapacityError
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.scheduler import Scheduler, SchedulerBusy


def running(scheduler, name):
    scheduler.enqueue(name, 'consult', on_busy='queue', retry_after_seconds=1, session_id=name)
    scheduler.set_state(name, 'QUEUED')
    assert scheduler.reserve_next(os.getpid(), 'task-owner') == name


def lanes(scheduler):
    with scheduler._connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM session_slots ORDER BY session_key')]


def test_unscoped_legacy_diagnostic_reproduces_busy_lane_binding(tmp_path):
    scheduler=Scheduler(tmp_path)
    running(scheduler,'active')
    scheduler.enqueue_browser_operation('legacy-probe',os.getpid(),'probe-owner')
    assert scheduler.operation_session('legacy-probe') == 'active'
    assert not scheduler.reserve_browser_operation('legacy-probe',os.getpid(),'probe-owner')
    scheduler.complete('legacy-probe','CANCELLED')


@pytest.mark.parametrize('resident_state', ['RUNNING', 'TIMEOUT'])
def test_diagnostic_has_independent_ephemeral_lane_with_available_capacity(tmp_path,resident_state):
    scheduler=Scheduler(tmp_path)
    running(scheduler,'retained')
    if resident_state == 'TIMEOUT':
        scheduler.complete('retained','TIMEOUT')
    before=lanes(scheduler)
    assert scheduler.enqueue_browser_operation('probe',os.getpid(),'owner',diagnostic=True)
    assert scheduler.operation_session('probe') == 'diagnostic-probe'
    diagnostic=next(row for row in lanes(scheduler) if row['task_id']=='probe')
    assert diagnostic['persistent'] == 0
    assert scheduler.status()['session_count'] == 2
    scheduler.complete('probe','COMPLETE')
    assert lanes(scheduler) == before


@pytest.mark.parametrize('idle', [False, True])
def test_four_residents_reject_diagnostic_without_borrowing_or_queueing(tmp_path,idle):
    scheduler=Scheduler(tmp_path)
    for index in range(4):
        running(scheduler,f's{index}')
        if idle:
            scheduler.complete(f's{index}','TIMEOUT')
    before=lanes(scheduler)
    with pytest.raises(SchedulerBusy) as exc:
        scheduler.enqueue_browser_operation('probe',os.getpid(),'owner',diagnostic=True)
    assert exc.value.info.reason == 'BROWSER_DIAGNOSTIC_BUSY'
    assert scheduler.status()['queue_length'] == 0
    assert lanes(scheduler) == before
    assert scheduler.status('probe')['scheduler_state'] is None


def test_diagnostic_respects_accepted_session_commitments_and_maintenance(tmp_path):
    scheduler=Scheduler(tmp_path)
    assert scheduler.acquire_external_slot('maintenance',os.getpid(),'maintenance-owner')
    with pytest.raises(SchedulerBusy) as exc:
        scheduler.enqueue_browser_operation('probe',os.getpid(),'owner',diagnostic=True)
    assert exc.value.info.active_task_id == 'maintenance'
    assert scheduler.status()['queue_length'] == 0
    scheduler.release_external_slot('maintenance','maintenance-owner')
    for index in range(4):
        scheduler.enqueue(f'queued{index}','consult',on_busy='queue',retry_after_seconds=1,session_id=f's{index}')
    with pytest.raises(SchedulerBusy):
        scheduler.enqueue_browser_operation('probe',os.getpid(),'owner',diagnostic=True)
    assert scheduler.status()['session_count'] == 0
    assert scheduler.status()['queue_length'] == 4


def test_diagnostic_never_targets_a_named_or_cleanup_session(tmp_path):
    scheduler=Scheduler(tmp_path)
    for kwargs in ({'session_id':'active'}, {'cleanup':True}):
        with pytest.raises(ValueError):
            scheduler.enqueue_browser_operation('probe',os.getpid(),'owner',diagnostic=True,**kwargs)
    assert scheduler.status()['queue_length'] == 0


def test_combined_provider_capacity_and_parallel_diagnostics_never_exceed_four(tmp_path):
    pool=tmp_path/'pool.sqlite3'
    chatgpt=Scheduler(tmp_path/'gpt',shared_pool_path=pool,namespace='chatgpt')
    gemini=Scheduler(tmp_path/'gemini',shared_pool_path=pool,namespace='gemini')
    running(gemini,'active-gemini')
    before=lanes(chatgpt)
    def probe(index):
        scheduler=chatgpt if index%2 else gemini
        try:
            scheduler.enqueue_browser_operation(f'probe{index}',os.getpid(),f'owner{index}',diagnostic=True)
            return scheduler,f'probe{index}'
        except SchedulerBusy as exc:
            assert exc.info.reason=='BROWSER_DIAGNOSTIC_BUSY'
            return None
    with ThreadPoolExecutor(max_workers=6) as executor:
        results=list(executor.map(probe,range(6)))
    admitted=[result for result in results if result]
    assert len(admitted)==3
    assert chatgpt.status()['session_count']==4
    for scheduler,task_id in admitted:
        scheduler.complete(task_id,'COMPLETE')
    assert lanes(chatgpt)==before
    assert gemini.session_active('active-gemini')=='active-gemini'


class ScopedDiagnosticBrowser:
    def __init__(self):
        self.scope=ContextVar('test-diagnostic-scope',default=None)
        self.observed=[]
        self.cleaned=[]
        self.fail=None
        self.ready=None
        self.release=None
        self.close=AsyncMock()

    @asynccontextmanager
    async def operation_scope(self,session_id=None,conversation_url=None):
        token=self.scope.set((session_id,conversation_url))
        try:
            yield
        finally:
            self.scope.reset(token)

    async def capabilities(self):
        self.observed.append(self.scope.get())
        if self.ready is not None:
            self.ready.set()
            await self.release.wait()
        if self.fail:
            raise self.fail
        return 'capabilities'

    async def browser_status(self):
        self.observed.append(self.scope.get())
        return {'ready':True}

    async def close_owned_pages(self,**kwargs):
        self.cleaned.append((self.scope.get(),kwargs))


def manager(tmp_path,browser):
    settings=replace(Settings.from_env(tmp_path),jobs_dir=tmp_path/'jobs',sessions_dir=tmp_path/'sessions',
        file_store_dir=tmp_path/'files',browser_profile_dir=tmp_path/'profile',shared_browser=False,
        browser_timeout_seconds=5,consult_poll_seconds=.01)
    return JobManager(settings,browser,export_owner=False)


@pytest.mark.parametrize('operation',['browser_capabilities','browser_status'])
def test_manager_diagnostic_uses_new_scope_and_releases_only_its_lease(tmp_path,operation):
    async def run():
        browser=ScopedDiagnosticBrowser()
        broker=manager(tmp_path,browser)
        await broker.start()
        try:
            session=broker.sessions.create('active')
            session.conversation_url='https://chatgpt.com/c/active'
            broker.sessions.save(session)
            running(broker.scheduler,'active')
            before=lanes(broker.scheduler)
            result=await asyncio.wait_for(getattr(broker,operation)(),1)
            assert result == ('capabilities' if operation=='browser_capabilities' else {'ready':True})
            scope,url=browser.observed[0]
            assert scope.startswith('diagnostic-browser_') and url is None
            assert browser.cleaned[0][0] == (scope,None)
            assert browser.cleaned[0][1] == {'keep_for_user':False,'retain_key':scope}
            assert lanes(broker.scheduler) == before
            assert broker.sessions.get(scope) is None
        finally:
            await broker.stop()
    asyncio.run(run())


@pytest.mark.parametrize('operation',['browser_capabilities','browser_status'])
def test_manager_saturated_diagnostic_is_fast_busy_and_never_enters_browser(tmp_path,operation):
    async def run():
        browser=ScopedDiagnosticBrowser()
        broker=manager(tmp_path,browser)
        await broker.start()
        try:
            for index in range(4):
                running(broker.scheduler,f'active{index}')
            before=lanes(broker.scheduler)
            with pytest.raises(SchedulerBusy) as exc:
                await asyncio.wait_for(getattr(broker,operation)(),.5)
            assert exc.value.info.reason == 'BROWSER_DIAGNOSTIC_BUSY'
            assert browser.observed == browser.cleaned == []
            assert broker.scheduler.status()['queue_length'] == 0
            assert lanes(broker.scheduler) == before
        finally:
            await broker.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['error','capacity','cancel'])
def test_manager_failed_or_cancelled_diagnostic_releases_ephemeral_lane(tmp_path,failure):
    async def run():
        browser=ScopedDiagnosticBrowser()
        broker=manager(tmp_path,browser)
        await broker.start()
        try:
            running(broker.scheduler,'active')
            before=lanes(broker.scheduler)
            if failure=='cancel':
                browser.ready,browser.release=asyncio.Event(),asyncio.Event()
                probe=asyncio.create_task(broker.browser_capabilities())
                await asyncio.wait_for(browser.ready.wait(),1)
                assert broker.scheduler.status()['session_count'] == 2
                probe.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await probe
            else:
                browser.fail=(BrowserCapacityError('four physical tabs') if failure=='capacity'
                              else RuntimeError('diagnostic failed'))
                with pytest.raises(type(browser.fail)):
                    await broker.browser_capabilities()
            assert lanes(broker.scheduler) == before
            assert len(browser.cleaned) == 1
            assert broker.scheduler.status()['queue_length'] == 0
        finally:
            await broker.stop()
    asyncio.run(run())
