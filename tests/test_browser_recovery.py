from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserPageNotReady, BrowserUnresponsiveError, ChatGPTBrowser
from chatgpt_oracle.browser_resources import ProfileResources, ResourceLockTimeout
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.browser import SubmitReceipt, BrowserSnapshot


def test_profile_lock_wait_has_deadline_and_can_be_reused(tmp_path):
    async def run():
        first = ProfileResources(tmp_path / 'profile', lock_timeout_seconds=0.04)
        second = ProfileResources(tmp_path / 'profile', lock_timeout_seconds=0.04)
        for method in ('locked', 'ui_locked'):
            async with getattr(first, method)():
                async def independent_owner():
                    async with getattr(second, method)():
                        pytest.fail('overlapping owners')
                with pytest.raises(ResourceLockTimeout):
                    await asyncio.create_task(independent_owner())
            async with getattr(second, method)():
                pass
    asyncio.run(run())


def test_stalled_ui_call_releases_lock_for_next_session(tmp_path):
    async def run():
        settings = replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path/'profile', browser_timeout_seconds=0.03)
        browser = ChatGPTBrowser(settings)
        page = SimpleNamespace(bring_to_front=AsyncMock())
        async with browser.operation_scope('a'):
            with pytest.raises(BrowserUnresponsiveError):
                async with browser._ui_transaction(page):
                    await asyncio.Event().wait()
        other = ProfileResources(settings.browser_profile_dir, lock_timeout_seconds=0.03)
        async with other.ui_locked():
            pass
    asyncio.run(run())


def test_readiness_error_diagnostics_cannot_escape_total_budget(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path/'profile', browser_timeout_seconds=0.04))
        browser._check_verification_challenge = AsyncMock()
        browser._require_composer = AsyncMock(side_effect=BrowserPageNotReady('unready'))
        async def stalled(*args):
            await asyncio.Event().wait()
        browser._page_requires_login = stalled
        class Page:
            reload = AsyncMock()
        page = Page()
        with pytest.raises(BrowserPageNotReady):
            await asyncio.wait_for(browser._prepare_submission_page(page, needs_selection=True, can_reload=True), 0.3)
        assert browser._scope.unresponsive
    asyncio.run(run())


class StalledBrowser:
    def __init__(self, *, poll=False):
        self.poll = poll
        self.sends = 0
        self.closes = 0
        self.unresponsive = False
    async def submit(self, *args, **kwargs):
        self.sends += 1
        if not self.poll:
            await asyncio.Event().wait()
        return SubmitReceipt('https://chatgpt.com/c/already-sent', 0)
    async def inspect(self, receipt):
        await asyncio.Event().wait()
    def mark_unresponsive(self):
        self.unresponsive = True
    async def close_owned_pages(self, **kwargs):
        self.closes += 1
    async def request_stop(self, receipt):
        return True
    async def close(self):
        pass


@pytest.mark.parametrize('poll', [False, True])
def test_stalled_submit_or_poll_ends_once_and_releases_operation(tmp_path, poll):
    async def run():
        browser = StalledBrowser(poll=poll)
        settings = replace(Settings.from_env(tmp_path), jobs_dir=tmp_path / 'jobs', sessions_dir=tmp_path / 'sessions',
            browser_timeout_seconds=0.03, consult_poll_seconds=0.01)
        manager = JobManager(settings, browser)
        try:
            job = await manager.submit('consult', 'single send', session_id='a')
            async def terminal():
                while manager.get(job.id).state != 'ERROR' or manager.scheduler.session_active('a'):
                    await asyncio.sleep(.005)
            await asyncio.wait_for(terminal(), 1)
            current = manager.get(job.id)
            assert current.error_code == 'BROWSER_UNRESPONSIVE'
            assert browser.sends == 1 and browser.unresponsive and browser.closes == 1
            assert current.conversation_url == ('https://chatgpt.com/c/already-sent' if poll else None)
            assert manager.scheduler.status()['active_count'] == 0
        finally:
            await manager.stop()
    asyncio.run(run())


def test_cancellation_resistant_driver_cleanup_invokes_owned_driver_abort(tmp_path):
    async def run():
        browser = StalledBrowser()
        released = asyncio.Event()
        aborts = []
        async def resistant():
            try:
                await asyncio.Event().wait()
            finally:
                await released.wait()
        async def abort():
            aborts.append(True)
            released.set()
        browser.abort_driver = abort
        manager = JobManager(replace(Settings.from_env(tmp_path), jobs_dir=tmp_path / 'jobs', sessions_dir=tmp_path / 'sessions'), browser)
        try:
            with pytest.raises(BrowserUnresponsiveError):
                await asyncio.wait_for(manager._call_browser(resistant, timeout=.03), 2)
            assert aborts == [True]
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize('peer', [False, True])
def test_unresponsive_cleanup_uses_registered_targets_and_preserves_budget(tmp_path, monkeypatch, peer):
    import httpx
    async def run():
        settings = replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path/'profile', browser_cdp_url='http://127.0.0.1:9222')
        browser = ChatGPTBrowser(settings)
        pages = {'owned': {'id': 'owned', 'type': 'page', 'url': 'https://chatgpt.com/c/a'}}
        if peer:
            pages['peer'] = {'id': 'peer', 'type': 'page', 'url': 'https://chatgpt.com/c/b'}
        peak = [len(pages)]
        actions = []
        def handle(request):
            actions.append((request.method, request.url.path))
            if request.url.path == '/json/list':
                return httpx.Response(200, json=list(pages.values()))
            if request.method == 'PUT' and request.url.path == '/json/new':
                assert str(request.url.query, 'utf-8') == 'about:blank'
                pages['idle'] = {'id': 'idle', 'type': 'page', 'url': 'about:blank'}
                peak[0] = max(peak[0], len(pages))
                return httpx.Response(200, json=pages['idle'])
            if request.url.path == '/json/close/owned':
                pages.pop('owned')
                return httpx.Response(200, text='Target is closing')
            raise AssertionError('unexpected target or request')
        original = httpx.AsyncClient
        monkeypatch.setattr('chatgpt_oracle.browser.httpx.AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handle), **kw))
        async with browser.operation_scope('a'):
            page = object()
            browser._owned_pages[page] = 'owned'
            async with browser._resources.locked() as conn:
                conn.execute("INSERT INTO pages(target_id,owner_pid,session_key,session_primary) VALUES('owned',1,'a',1)")
                if peer:
                    conn.execute("INSERT INTO pages(target_id,owner_pid,session_key,session_primary) VALUES('peer',1,'b',1)")
            browser.mark_unresponsive()
            await browser.close_owned_pages(keep_for_user=False, retain_key='a')
            assert not browser._owned_pages
        assert set(pages) == ({'peer'} if peer else {'idle'})
        assert peak[0] <= 2
        assert ('GET', '/json/close/peer') not in actions
        async with browser._resources.locked() as conn:
            assert [row[0] for row in conn.execute('SELECT target_id FROM pages')] == (['peer'] if peer else [])
    asyncio.run(run())


def test_cancel_during_inspection_is_not_overwritten_by_running_snapshot(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        browser = StalledBrowser(poll=True)
        inspections = []
        async def inspect(receipt):
            inspections.append(True)
            if len(inspections) == 1:
                entered.set()
                await release.wait()
                return BrowserSnapshot('RUNNING', receipt.conversation_url)
            return BrowserSnapshot('COMPLETE', receipt.conversation_url, result_text='answer')
        browser.inspect = inspect
        manager = JobManager(replace(Settings.from_env(tmp_path), jobs_dir=tmp_path/'jobs', sessions_dir=tmp_path/'sessions',
            browser_timeout_seconds=1, consult_poll_seconds=.01), browser)
        try:
            job = await manager.submit('consult', 'once', session_id='a')
            await asyncio.wait_for(entered.wait(), 1)
            await manager.cancel(job.id)
            release.set()
            async def cancelled():
                while manager.get(job.id).state != 'CANCELLED':
                    await asyncio.sleep(.005)
            await asyncio.wait_for(cancelled(), 1)
            assert browser.sends == 1
            assert manager.get(job.id).cancellation_stop_verified is True
            assert not manager.get(job.id).result_path
        finally:
            release.set()
            await manager.stop()
    asyncio.run(run())


def test_new_page_preparation_is_serial_but_generation_does_not_hold_ui(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path/'profile', browser_timeout_seconds=1))
        entered, release = asyncio.Event(), asyncio.Event()
        inflight, peak, order = [0], [0], []
        async def fresh():
            key = browser._scope.session_id
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
            order.append(key)
            if key == 'a':
                entered.set()
                await release.wait()
            inflight[0] -= 1
            class Page:
                url = 'https://chatgpt.com/'
                bring_to_front = AsyncMock()
            return Page()
        browser._fresh_conversation_page = fresh
        browser._prepare_submission_page = AsyncMock(return_value=None)
        # This case isolates the shared preparation lock. Real guarded editor
        # writes are exercised separately against native DOM fixtures.
        browser._fill_prepared_draft = AsyncMock()
        browser._apply_selection = AsyncMock(return_value={'resolved_model':None,'resolved_reasoning_effort':'Extra High','selection_source':'explicit','selection_verified_at':'verified'})
        browser._verify_selection_after_upload = AsyncMock()
        browser._upload_files = AsyncMock(return_value=[])
        browser._find_composer = AsyncMock(return_value=SimpleNamespace(fill=AsyncMock(), evaluate=AsyncMock(return_value={"text": "", "prompt_text": "", "has_mention": False})))
        browser._check_composer_draft = AsyncMock(return_value=browser._find_composer.return_value)
        browser._read_turn_surface = AsyncMock(return_value={'messages':[]})
        browser._confirm_submission = AsyncMock()
        async def send(page):
            page.url = 'https://chatgpt.com/c/' + browser._scope.session_id
        browser._click_send = send
        browser._wait_for_conversation_url = AsyncMock()
        async def submit(key):
            async with browser.operation_scope(key):
                return await browser.submit('consult', key, quality='extra_high', dedicated_page=True)
        first = asyncio.create_task(submit('a'))
        await entered.wait()
        second = asyncio.create_task(submit('b'))
        await asyncio.sleep(.02)
        assert order == ['a']
        release.set()
        receipts = await asyncio.gather(first, second)
        assert peak == [1] and order == ['a','b']
        assert browser._verify_selection_after_upload.await_count == 2
        assert [r.conversation_url for r in receipts] == ['https://chatgpt.com/c/a','https://chatgpt.com/c/b']
        # Submission returned while both remote replies can still be generating.
        async with browser._resources.ui_locked():
            pass
    asyncio.run(run())


def test_driver_abort_terminates_owned_pipe_and_consumes_disconnect_future(tmp_path):
    from unittest.mock import Mock
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path/'profile'))
        class ErrorFuture(asyncio.Future):
            retrieved = 0
            def exception(self):
                self.retrieved += 1
                return super().exception()
        failure = ErrorFuture()
        failure.set_exception(Exception('driver pipe closed'))
        process = SimpleNamespace(returncode=None, kill=Mock(), wait=AsyncMock(return_value=0))
        transport = SimpleNamespace(_proc=process, on_error_future=failure)
        playwright = SimpleNamespace(_impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport)), stop=AsyncMock())
        chrome = SimpleNamespace(close=AsyncMock())
        browser._playwright = playwright
        browser._browser = chrome
        await browser.abort_driver()
        process.kill.assert_called_once()
        chrome.close.assert_not_awaited()
        assert failure.retrieved == 1 and browser._playwright is None and browser._context is None
    asyncio.run(run())
