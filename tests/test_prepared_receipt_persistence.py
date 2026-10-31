from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, BrowserTaskError, BrowserUnresponsiveError, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.oracle_protocol import normalized_text_sha256


class EarlyReceiptBrowser:
    requires_turn_receipts = True

    def __init__(self, failure):
        self.failure = failure
        self.closed = []
        self.send_count = 0
        self.manager = None
        self.persisted_before_send = None

    async def submit(self, mode, prompt, *, quality='auto', on_submission_prepared=None, **kwargs):
        if self.failure == 'selection':
            raise BrowserTaskError('model menu vanished before preparation')
        receipt = SubmitReceipt('https://chatgpt.com/', 0,
            resolved_model='visible model', resolved_reasoning_effort='Extra High',
            selection_source='visible_picker', selection_verified_at='2026-10-16T00:00:00Z',
            submitted_prompt_sha256=normalized_text_sha256(prompt),
            submission_state='unconfirmed', attachments=[{'filename':'fixture.zip','verified':True}])
        assert on_submission_prepared
        await on_submission_prepared(receipt)
        jobs=[j for j in self.manager.store.all() if j.sending_text==prompt]
        assert len(jobs)==1
        self.persisted_before_send=jobs[0]
        assert jobs[0].state=='SEND_PENDING'
        assert jobs[0].submitted_prompt_sha256==receipt.submitted_prompt_sha256
        assert jobs[0].resolved_reasoning_effort=='Extra High'
        assert jobs[0].conversation_url is None
        self.send_count+=1
        if self.failure=='url':
            raise BrowserTaskError('formal conversation URL unavailable after one click')
        if self.failure=='driver':
            raise BrowserUnresponsiveError('driver timed out after one click')
        receipt.conversation_url='https://chatgpt.com/c/fixture'
        receipt.current_user_message_id='u1'
        receipt.current_assistant_message_id='a1'
        receipt.submission_state='confirmed'
        receipt.submission_verified_at='2026-10-16T00:00:01Z'
        return receipt

    async def inspect(self, receipt):
        return BrowserSnapshot('COMPLETE',receipt.conversation_url,result_text='test answer')

    async def close_owned_pages(self, **kwargs):
        self.closed.append(kwargs)

    async def close(self):
        pass


@pytest.mark.parametrize("model,effort", [("GPT-6", None), (None, "High"), (None, None)])
def test_auto_quality_preserves_explicit_selection_provenance(tmp_path, model, effort):
    from chatgpt_oracle.models import Job
    manager = JobManager(Settings.from_env(tmp_path), EarlyReceiptBrowser(None))
    job = Job.create("consult", "test selection metadata", quality="auto", model=model, reasoning_effort=effort)
    receipt = SubmitReceipt("https://chatgpt.com/", 0, selection_source="explicit",
        resolved_model=model, resolved_reasoning_effort=effort)
    manager._remember_submission_receipt(job, receipt)
    assert job.selection_source == ("explicit" if model or effort else "default")


@pytest.mark.parametrize('failure',['url','driver','selection',None])
def test_receipt_survives_exception_and_only_formal_url_enters_session(tmp_path,failure):
    async def run():
        value=replace(Settings.from_env(tmp_path),jobs_dir=tmp_path/'jobs',sessions_dir=tmp_path/'sessions',
                      browser_profile_dir=tmp_path/'profile',consult_poll_seconds=.01)
        browser=EarlyReceiptBrowser(failure)
        manager=JobManager(value,browser);browser.manager=manager
        try:
            job=await manager.submit('consult','test prompt',session_id='s1',request_id='r1')
            async with asyncio.timeout(3):
                while manager.get(job.id).state not in {'ERROR','COMPLETE'} or manager.scheduler.session_active('s1'):
                    await asyncio.sleep(.01)
            result=manager.get(job.id)
            if failure:
                assert result.state=='ERROR'
                assert browser.closed[-1]['keep_for_user'] is True
                assert manager.sessions.get('s1').conversation_url is None
                if failure!='selection':
                    assert result.submission_state=='unconfirmed'
                    assert result.submitted_prompt_sha256==normalized_text_sha256(result.sending_text)
                    assert result.resolved_reasoning_effort=='Extra High'
                    assert result.attachments[0]['verified'] is True
                    assert browser.send_count==1
                else:
                    assert browser.send_count==0
            else:
                assert result.state=='COMPLETE' and result.submission_state=='confirmed'
                assert result.conversation_url=='https://chatgpt.com/c/fixture'
                assert manager.sessions.get('s1').conversation_url==result.conversation_url
                assert browser.send_count==1
        finally:
            await manager.stop()
    asyncio.run(run())


def test_unresponsive_failed_page_is_retained_without_driver_destruction(tmp_path):
    from unittest.mock import AsyncMock
    from test_browser_resources import Context, browser
    async def run():
        context=Context();controller=browser(tmp_path,context)
        page=await controller._new_page()
        controller.mark_unresponsive()
        controller.discard_unresponsive_pages=AsyncMock(side_effect=AssertionError('evidence destroyed'))
        await controller.close_owned_pages(keep_for_user=True,retain_key='failed-session')
        assert not page.is_closed()
        async with controller._resources.locked() as conn:
            row=conn.execute('SELECT owner_pid,retained,retain_key FROM pages').fetchone()
            assert row==(None,1,'failed-session')
        controller.discard_unresponsive_pages.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('arrives',[True,False])
def test_late_acknowledgement_observes_same_task_without_resubmission(tmp_path,arrives):
    class LateBrowser(EarlyReceiptBrowser):
        def __init__(self):
            super().__init__(None);self.polls=0
        async def submit(self,mode,prompt,*,on_submission_prepared=None,**kwargs):
            receipt=SubmitReceipt('https://chatgpt.com/',0,
                submitted_prompt_sha256=normalized_text_sha256(prompt),submission_state='unconfirmed')
            await on_submission_prepared(receipt)
            self.send_count+=1
            return receipt
        async def inspect_submission(self,receipt):
            self.polls+=1
            assert self.manager.store.all()[0].state=='SEND_PENDING'
            assert not self.manager.public(self.manager.store.all()[0].id)['terminal']
            if arrives and self.polls>=3:
                receipt.conversation_url='https://chatgpt.com/c/late'
                receipt.current_user_message_id='late-user'
                receipt.current_assistant_message_id='late-answer'
                receipt.submission_state='confirmed'
                receipt.submission_verified_at='2026-10-16T00:00:05Z'
                return BrowserSnapshot('COMPLETE',receipt.conversation_url,result_text='late answer',submission_state='confirmed')
            return BrowserSnapshot('SEND_PENDING',None,submission_state='unconfirmed')
    async def run():
        value=replace(Settings.from_env(tmp_path),jobs_dir=tmp_path/'jobs',sessions_dir=tmp_path/'sessions',
            browser_profile_dir=tmp_path/'profile',consult_poll_seconds=.01,consult_timeout_seconds=.5 if arrives else .1)
        browser=LateBrowser();manager=JobManager(value,browser);browser.manager=manager
        try:
            job=await manager.submit('consult','one unique question',session_id='late-session',request_id='late-request')
            async with asyncio.timeout(3):
                while manager.get(job.id).state not in {'COMPLETE','TIMEOUT','ERROR'} or manager.scheduler.session_active('late-session'):
                    await asyncio.sleep(.005)
            result=manager.get(job.id)
            assert browser.send_count==1 and result.id==job.id and result.request_id=='late-request'
            assert result.state==('COMPLETE' if arrives else 'TIMEOUT')
            assert result.submission_state==('confirmed' if arrives else 'unconfirmed')
            if arrives:assert result.current_user_message_id=='late-user'
            else:assert browser.closed[-1]['keep_for_user'] is True
        finally:await manager.stop()
    asyncio.run(run())
