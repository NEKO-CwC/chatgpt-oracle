"""Durable same-turn runtime recovery, with no real browser or upstream IO."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from chatgpt_oracle.browser import BrowserNeedsUserAction, BrowserSnapshot, BrowserTaskError, SubmitReceipt
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job, public_job, utc_now
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from test_jobs import settings


URL = 'https://chatgpt.com/c/runtime-recovery-original'


def runtime_error():
    return BrowserSnapshot('RECOVERABLE_ERROR', URL, runtime_error_code='cloudflare_challenge',
                           result_text='STALE SNAPSHOT MUST NOT BECOME AN ANSWER', generation_active=False)


def answer():
    return BrowserSnapshot('COMPLETE', URL, result_text='original turn answer', generation_active=False)


class RecoveryBrowser:
    requires_turn_receipts = True

    def __init__(self, snapshots, recover=None):
        self.snapshots = list(snapshots)
        self.recover = recover
        self.manager = None
        self.job_id = None
        self.sent = []
        self.recovery_calls = []
        self.actions = []
        self.receipts = []
        self.stop_calls = 0

    async def submit(self, mode, prompt, **kwargs):
        self.sent.append((mode, prompt, kwargs))
        return SubmitReceipt(URL, 1, selected_model=kwargs.get('model'),
            resolved_model='chosen-model', resolved_reasoning_effort='Extra High', selection_source='explicit',
            selection_verified_at=utc_now(), submitted_prompt_sha256=normalized_text_sha256(prompt),
            user_message_ids_before=['u-old'], assistant_message_ids_before=['a-old'],
            current_user_message_id='u-current', submission_state='confirmed', submission_verified_at=utc_now())

    async def continue_conversation(self, *_args, **_kwargs):
        raise AssertionError('Runtime recovery must never create a continuation turn')

    async def inspect(self, receipt):
        self.receipts.append(receipt)
        return self.snapshots.pop(0) if self.snapshots else answer()

    async def recover_runtime_error(self, receipt, *, attempt, on_recovery_prepared):
        self.recovery_calls.append(attempt)
        if self.recover:
            return await self.recover(receipt, attempt, on_recovery_prepared)
        action = 'retry_site_alert' if attempt == 1 else 'reload_conversation'
        await on_recovery_prepared(action)
        saved = self.manager.store.get(self.job_id)
        assert saved.runtime_recovery_attempts == attempt
        assert saved.runtime_recovery_actions[-1]['action'] == action
        assert saved.runtime_recovery_actions[-1]['prepared_at']
        assert saved.state == 'RUNNING'
        self.actions.append(action)
        return True

    async def request_stop(self, _receipt):
        self.stop_calls += 1
        return True

    async def close(self):
        pass


def setup(tmp_path, browser):
    manager = JobManager(replace(settings(tmp_path), consult_timeout_seconds=3), browser)
    browser.manager = manager
    return manager


def seed(manager, browser, *, attempts=0):
    job = Job.create('consult', 'original caller prompt', session_id='runtime-recovery',
                     model='requested-model', quality='extra_high', reasoning_effort='extra_high',
                     file_refs=['original-file-ref'], upload_ids=['original-upload-id'],
                     attachments=[{'verified': True, 'filename': 'original.zip'}])
    job.request_id = 'original-request'
    job.state = 'RUNNING'
    job.conversation_url = URL
    job.sending_text = '【CODEX_ORACLE】\noriginal frozen body'
    job.submitted_prompt_sha256 = normalized_text_sha256(job.sending_text)
    job.user_message_ids_before = ['u-old']
    job.assistant_message_ids_before = ['a-old']
    job.current_user_message_id = 'u-current'
    job.submission_state = 'confirmed'
    job.submission_verified_at = utc_now()
    job.resolved_model = 'chosen-model'
    job.resolved_reasoning_effort = 'Extra High'
    job.selection_source = 'explicit'
    job.selection_verified_at = utc_now()
    job.runtime_recovery_attempts = attempts
    job.runtime_recovery_actions = [dict(attempt=i, action='retry_site_alert', prepared_at=utc_now())
                                    for i in range(1, attempts + 1)]
    manager.store.save(job)
    manager.sessions.touch(job.session_id, conversation_url=URL, last_task_id=job.id)
    browser.job_id = job.id
    return job


def frozen_identity(job):
    keys = ['id','request_id','prompt','model','quality','reasoning_effort','allow_downgrade','session_id',
            'file_refs','upload_ids','attachments','conversation_url','sending_text','submitted_prompt_sha256',
            'user_message_ids_before','assistant_message_ids_before','current_user_message_id',
            'submission_state','submission_verified_at','resolved_model','resolved_reasoning_effort',
            'selection_source','selection_verified_at']
    return {k: getattr(job,k) for k in keys}


def test_old_jobs_default_to_an_independent_empty_recovery_budget():
    data = dict(id='cgo_old', mode='consult', prompt='old')
    first, second = Job.from_dict(data), Job.from_dict(data)
    assert first.runtime_recovery_attempts == 0
    assert first.runtime_recovery_actions == []
    first.runtime_recovery_actions.append({'action':'fixture'})
    assert second.runtime_recovery_actions == []
    assert public_job(second)['runtime_recovery_attempts'] == 0
    assert public_job(second)['runtime_recovery_actions'] == []


def test_recovery_reserves_before_action_and_keeps_original_turn_and_inputs(tmp_path):
    async def run():
        browser = RecoveryBrowser([runtime_error(), answer()])
        manager = setup(tmp_path,browser)
        job = seed(manager,browser)
        original = frozen_identity(job)
        try:
            await manager._process_body(job)
            saved = manager.get(job.id)
            assert saved.state == 'COMPLETE'
            assert frozen_identity(saved) == original
            assert browser.sent == [] and browser.actions == ['retry_site_alert']
            assert browser.recovery_calls == [1] and len(browser.receipts) == 2
            assert saved.runtime_recovery_attempts == 1
            result = manager.store.read_result(saved)
            assert 'original turn answer' in result and 'STALE SNAPSHOT' not in result
            assert public_job(saved)['runtime_recovery_actions'][0]['runtime_error_code']=='cloudflare_challenge'
        finally:
            await manager.stop()
    asyncio.run(run())


def test_runtime_error_disappearance_uses_no_action_and_observes_again(tmp_path):
    async def gone(_receipt,_attempt,_prepared):
        return False
    async def run():
        browser=RecoveryBrowser([runtime_error(),answer()],gone)
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='COMPLETE' and saved.runtime_recovery_attempts==0
            assert saved.runtime_recovery_actions==[] and browser.actions==[]
            assert len(browser.receipts)==2
        finally:
            await manager.stop()
    asyncio.run(run())


def test_new_task_sends_once_across_runtime_recovery(tmp_path):
    async def run():
        browser=RecoveryBrowser([runtime_error(),answer()]);manager=setup(tmp_path,browser)
        async def recover(receipt,_attempt,prepared):
            await prepared('retry_site_alert')
            saved=next(j for j in manager.store.all() if j.current_user_message_id==receipt.current_user_message_id)
            assert saved.request_id=='only-request' and saved.runtime_recovery_attempts==1
            browser.actions.append('retry_site_alert')
            return True
        browser.recover=recover
        try:
            job=await manager.submit('consult','one actual send',session_id='fresh-recovery',
                                     request_id='only-request',model='requested-model',quality='extra_high')
            for _ in range(200):
                saved=manager.get(job.id)
                if saved.state=='COMPLETE':break
                await asyncio.sleep(.01)
            assert saved.state=='COMPLETE' and saved.runtime_recovery_attempts==1
            assert len(browser.sent)==1 and browser.sent[0][2]['model']=='requested-model'
            assert browser.sent[0][2]['allow_downgrade'] is False
            assert saved.request_id=='only-request' and saved.current_user_message_id=='u-current'
        finally:
            await manager.stop()
    asyncio.run(run())


def test_two_prepared_actions_exhaust_budget_without_a_third_recovery(tmp_path):
    async def run():
        browser=RecoveryBrowser([runtime_error(),runtime_error(),runtime_error()])
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='ERROR' and saved.error_code=='RUNTIME_RECOVERY_EXHAUSTED'
            assert saved.runtime_recovery_attempts==2 and browser.recovery_calls==[1,2]
            assert browser.actions==['retry_site_alert','reload_conversation'] and browser.sent==[]
            assert saved.user_action is None and saved.user_input_kind is None
            assert saved.result_path is None
        finally:
            await manager.stop()
    asyncio.run(run())


def test_action_failure_is_durable_and_not_repeated_in_the_same_process(tmp_path):
    async def run():
        browser=RecoveryBrowser([runtime_error(),answer()])
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        async def failed(_receipt,_attempt,prepared):
            await prepared('retry_site_alert')
            assert manager.get(job.id).runtime_recovery_attempts==1
            browser.actions.append('retry_site_alert')
            raise BrowserTaskError('ambiguous browser action failure')
        browser.recover=failed
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='ERROR' and saved.error_code=='RUNTIME_RECOVERY_FAILED'
            assert saved.runtime_recovery_attempts==1
            assert browser.recovery_calls==[1] and browser.actions==['retry_site_alert']
            assert browser.sent==[] and len(browser.receipts)==1
        finally:
            await manager.stop()
    asyncio.run(run())


def test_broker_restart_keeps_consumed_attempt_and_only_polls_original_turn(tmp_path):
    async def run():
        entered=asyncio.Event()
        first=RecoveryBrowser([runtime_error()])
        manager=setup(tmp_path,first);job=seed(manager,first)
        async def interrupted(_receipt,_attempt,prepared):
            await prepared('retry_site_alert')
            first.actions.append('retry_site_alert');entered.set()
            await asyncio.Event().wait()
        first.recover=interrupted
        # Run through the real worker/shutdown path, rather than reconstructing
        # a fresh counter in a second test manager.
        manager.scheduler.enqueue(job.id,job.mode,session_id=job.session_id,on_busy='queue',retry_after_seconds=1)
        manager.scheduler.set_state(job.id,'QUEUED')
        await manager.start()
        await asyncio.wait_for(entered.wait(),2)
        await manager.stop()
        persisted=manager.get(job.id)
        assert persisted.state=='RUNNING' and persisted.runtime_recovery_attempts==1
        assert manager.scheduler.status(job.id)['scheduler_state']=='QUEUED'
        second=RecoveryBrowser([runtime_error(),runtime_error()])
        restarted=setup(tmp_path,second);second.job_id=job.id
        try:
            await restarted.start()
            for _ in range(200):
                saved=restarted.get(job.id)
                if saved.state=='ERROR':break
                await asyncio.sleep(.01)
            assert saved.error_code=='RUNTIME_RECOVERY_EXHAUSTED'
            assert saved.runtime_recovery_attempts==2 and second.recovery_calls==[2]
            assert second.actions==['reload_conversation']
            assert first.sent==second.sent==[] and saved.current_user_message_id=='u-current'
            assert saved.submitted_prompt_sha256==job.submitted_prompt_sha256
        finally:
            await restarted.stop()
    asyncio.run(run())


def test_cancel_before_preparation_blocks_the_runtime_action(tmp_path):
    async def run():
        browser=RecoveryBrowser([runtime_error()])
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        async def cancel_then_prepare(_receipt,_attempt,prepared):
            await manager.cancel(job.id)
            await prepared('retry_site_alert')
            browser.actions.append('must not happen')
            return True
        browser.recover=cancel_then_prepare
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='CANCELLED' and saved.cancellation_stop_verified is True
            assert saved.runtime_recovery_attempts==0 and browser.actions==[]
            assert browser.stop_calls==1 and browser.sent==[]
        finally:
            await manager.stop()
    asyncio.run(run())


def test_cancel_after_preparation_retains_the_consumed_budget(tmp_path):
    async def run():
        browser=RecoveryBrowser([runtime_error()]);manager=setup(tmp_path,browser);job=seed(manager,browser)
        async def cancel_after_prepare(_receipt,_attempt,prepared):
            await prepared('retry_site_alert')
            await manager.cancel(job.id)
            await asyncio.Event().wait()
            browser.actions.append('must not happen')
        browser.recover=cancel_after_prepare
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='CANCELLED' and saved.cancellation_stop_verified is True
            assert saved.runtime_recovery_attempts==1 and len(saved.runtime_recovery_actions)==1
            assert browser.actions==[] and browser.stop_calls==1 and browser.sent==[]
        finally:
            await manager.stop()
    asyncio.run(run())


def test_failed_durable_reservation_prevents_the_browser_action(tmp_path):
    async def run():
        browser=RecoveryBrowser([runtime_error()]);manager=setup(tmp_path,browser);job=seed(manager,browser)
        save=manager.store.save
        def unavailable(current):
            if current.runtime_recovery_attempts and current.state=='RUNNING':
                raise OSError('fixture reservation write failed')
            save(current)
        manager.store.save=unavailable
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='ERROR' and saved.error_code=='RUNTIME_RECOVERY_FAILED'
            assert browser.actions==[] and browser.recovery_calls==[1]
        finally:
            manager.store.save=save
            await manager.stop()
    asyncio.run(run())


def test_one_browser_operation_cannot_prepare_a_second_action(tmp_path):
    async def double_prepare(_receipt,_attempt,prepared):
        await prepared('retry_site_alert')
        await prepared('reload_conversation')
        raise AssertionError('second reservation should have failed')
    async def run():
        browser=RecoveryBrowser([runtime_error()],double_prepare);manager=setup(tmp_path,browser);job=seed(manager,browser)
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='ERROR' and saved.error_code=='RUNTIME_RECOVERY_FAILED'
            assert saved.runtime_recovery_attempts==1 and len(saved.runtime_recovery_actions)==1
            assert browser.recovery_calls==[1]
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize('reason',['login_required','verification_required'])
def test_real_manual_challenges_never_trigger_runtime_recovery(tmp_path,reason):
    async def run():
        browser=RecoveryBrowser([BrowserSnapshot('NEEDS_USER_INPUT',URL,user_action=reason,user_input_kind='manual_browser')])
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='NEEDS_USER_INPUT' and saved.user_action==reason
            assert browser.recovery_calls==[] and saved.runtime_recovery_attempts==0
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize('reason,expected',[('verification_required','NEEDS_USER_INPUT'),('unknown_overlay','ERROR')])
def test_recovery_only_routes_real_challenges_to_the_user(tmp_path,reason,expected):
    async def blocked(_receipt,_attempt,_prepared):
        raise BrowserNeedsUserAction(reason,'fixture instructions')
    async def run():
        browser=RecoveryBrowser([runtime_error()],blocked)
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state==expected and saved.runtime_recovery_attempts==0
            if expected=='ERROR':
                assert saved.error_code=='RUNTIME_RECOVERY_UNSAFE' and saved.user_action is None
        finally:
            await manager.stop()
    asyncio.run(run())


def test_unsafe_or_unsupported_recovery_is_a_service_error(tmp_path):
    async def run():
        browser=RecoveryBrowser([runtime_error()]);browser.recover_runtime_error=None
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='ERROR' and saved.error_code=='RUNTIME_RECOVERY_UNSUPPORTED'
            assert saved.user_action is None and saved.runtime_recovery_attempts==0
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize('with_callback,result',[(False,True),(True,False),(False,None)])
def test_recovery_contract_violation_stops_without_repeating_actions(tmp_path,with_callback,result):
    async def mismatched(_receipt,_attempt,prepared):
        if with_callback:await prepared('retry_site_alert')
        return result
    async def run():
        browser=RecoveryBrowser([runtime_error()],mismatched)
        manager=setup(tmp_path,browser);job=seed(manager,browser)
        try:
            await manager._process_body(job)
            saved=manager.get(job.id)
            assert saved.state=='ERROR' and saved.error_code=='RUNTIME_RECOVERY_UNCONFIRMED'
            assert saved.runtime_recovery_attempts==int(with_callback)
            assert browser.recovery_calls==[1] and browser.sent==[]
        finally:
            await manager.stop()
    asyncio.run(run())
