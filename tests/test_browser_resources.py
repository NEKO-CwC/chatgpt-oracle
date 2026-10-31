from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from chatgpt_oracle.browser import BrowserCapacityError, BrowserNeedsUserAction, BrowserTaskError, ChatGPTBrowser
from chatgpt_oracle.config import Settings
from playwright.async_api import Error as PlaywrightError


class Page:
    def __init__(self, context, target):
        self.context, self.target = context, target
        self.url = 'about:blank'
        self.closed = False
        self.goto = AsyncMock()
        self.bring_to_front = AsyncMock()
        self.time_origin = object()
        self.draft = "unsent user draft"
        self.on = Mock()
        self.remove_listener = Mock()
        self.wait_for_timeout = AsyncMock()

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        if self in self.context.pages:
            self.context.pages.remove(self)


class Context:
    def __init__(self):
        self.pages = []
        self.next_id = 0
        self.peak = 0

    async def new_page(self):
        await asyncio.sleep(0)
        self.next_id += 1
        page = Page(self, str(self.next_id))
        self.pages.append(page)
        self.peak = max(self.peak, len(self.pages))
        return page

    async def new_cdp_session(self, page):
        return SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':page.target}}), detach=AsyncMock())


def browser(tmp_path, context):
    instance = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path/'profile'))
    instance._context = context
    instance.start = AsyncMock()
    return instance


def test_shared_profile_never_allocates_a_fifth_page(tmp_path):
    async def run():
        context = Context()
        home = await context.new_page()
        first, second = browser(tmp_path,context), browser(tmp_path,context)
        results = await asyncio.gather(*(b._new_page() for b in [first,second,first,second,first]), return_exceptions=True)
        assert sum(isinstance(r,BrowserCapacityError) for r in results) == 1
        assert home in results
        assert len(context.pages) == context.peak == 4
        await first.close_owned_pages()
        await second.close_owned_pages()
        assert not context.pages
        async with first._resources.locked() as conn:
            assert conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 0
    asyncio.run(run())


def test_capacity_can_be_reused_after_operation_finishes(tmp_path):
    async def run():
        context = Context(); instance = browser(tmp_path,context)
        for _ in range(8):
            pages = [await instance._new_page() for _ in range(4)]
            with pytest.raises(BrowserCapacityError):
                await instance._new_page()
            await instance.close_owned_pages()
            assert not context.pages
            assert all(p.is_closed() for p in pages)
        assert context.peak == 4
    asyncio.run(run())


def test_cdp_target_count_takes_precedence_over_stale_local_page_list(tmp_path):
    async def run():
        context=Context();instance=browser(tmp_path,context)
        cdp=SimpleNamespace(send=AsyncMock(return_value={'targetInfos':[{'type':'page'}]*4+[{'type':'iframe'}]}),detach=AsyncMock())
        instance._browser=SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))
        assert not context.pages
        with pytest.raises(BrowserCapacityError):await instance._new_page()
        assert not context.pages
        cdp.detach.assert_awaited_once()
    asyncio.run(run())


def test_retained_takeover_counts_against_cap_and_session_close_releases_it(tmp_path):
    async def run():
        context = Context(); instance = browser(tmp_path,context)
        retained = await instance._new_page()
        await instance.close_owned_pages(keep_for_user=True,retain_key='session-a')
        other = browser(tmp_path,context)
        for _ in range(3):await other._new_page()
        with pytest.raises(BrowserCapacityError):await other._new_page()
        await other.close_retained_pages('session-a')
        assert retained.is_closed()
        await other.close_owned_pages()
        assert not context.pages
    asyncio.run(run())


def test_dead_owner_is_reaped_but_live_owner_and_manual_page_survive(tmp_path):
    async def run():
        context = Context(); instance = browser(tmp_path,context)
        orphan, live, manual = [await context.new_page() for _ in range(3)]
        manual.url = 'https://example.test/manual'
        async with instance._resources.locked() as conn:
            conn.execute('INSERT INTO pages(target_id,owner_pid,retained,retain_key) VALUES(?,?,0,NULL)',(orphan.target,0))
            conn.execute('INSERT INTO pages(target_id,owner_pid,retained,retain_key) VALUES(?,?,0,NULL)',(live.target,os.getpid()))
        own = await instance._new_page()
        assert orphan.is_closed()
        assert set(context.pages) == {live,manual,own}
        await instance.close_owned_pages()
        assert set(context.pages) == {live,manual}
    asyncio.run(run())


def test_pre_url_error_still_has_an_owned_handle(tmp_path):
    async def run():
        context = Context(); instance = browser(tmp_path,context)
        instance._target_id = AsyncMock(side_effect=RuntimeError('target lookup failed'))
        with pytest.raises(RuntimeError):await instance._new_page()
        assert len(context.pages) == 1
        await instance.close_owned_pages()
        assert not context.pages
    asyncio.run(run())


def test_closing_one_session_preserves_other_page_ownership(tmp_path):
    async def run():
        context=Context();instance=browser(tmp_path,context)
        target=await instance._new_page();target.url='https://chatgpt.com/c/a'
        other=await instance._new_page();other.url='https://chatgpt.com/c/b'
        assert await instance.close_conversation_tabs(target.url) is True
        assert target.is_closed() and not other.is_closed()
        async with instance._resources.locked() as conn:
            assert conn.execute('SELECT target_id FROM pages').fetchall()==[(other.target,)]
        await instance.close_retained_pages('unrelated-session')
        async with instance._resources.locked() as conn:
            assert conn.execute('SELECT target_id FROM pages').fetchall()==[(other.target,)]
        assert not other.is_closed()
        await instance.close_owned_pages()
        await instance.close_retained_pages('empty-context')
        assert await instance.close_conversation_tabs(target.url) is False
    asyncio.run(run())


def test_four_scopes_retain_independent_pages_and_continue_without_navigation(tmp_path):
    async def run():
        context = Context()
        blank = await context.new_page()
        instance = browser(tmp_path, context)
        pages = {}
        ready = asyncio.Event()

        async def first_turn(index):
            async with instance.operation_scope(f'session-{index}', None):
                page = await instance._new_page()
                page.url = f'https://chatgpt.com/c/{index}'
                pages[index] = page
                if len(pages) == 4:
                    ready.set()
                await ready.wait()
                assert list(instance._owned_pages) == [page]
                await instance.close_owned_pages(keep_for_user=True, retain_key=f'session-{index}')

        await asyncio.wait_for(asyncio.gather(*(first_turn(i) for i in range(4))), 3)
        assert blank in pages.values()
        assert context.peak == len(context.pages) == 4
        async with instance.operation_scope('session-fifth', None):
            with pytest.raises(BrowserCapacityError):
                await instance._new_page()
        for index, page in pages.items():
            origin = page.time_origin
            async with instance.operation_scope(f'session-{index}', page.url):
                assert await instance._conversation_page(page.url) is page
                assert await instance._page() is page
                assert page.time_origin is origin
                page.goto.assert_not_awaited()
                page.bring_to_front.assert_not_awaited()
                await instance.close_owned_pages(keep_for_user=True, retain_key=f'session-{index}')
        await instance.close_retained_pages('session-1')
        assert pages[1].is_closed()
        assert all(not page.is_closed() for i, page in pages.items() if i != 1)
        async with instance.operation_scope('session-fifth', None):
            await instance._new_page()
            await instance.close_owned_pages()
        assert context.peak == 4
        for index in (0, 2, 3):
            await instance.close_retained_pages(f'session-{index}')
        assert not context.pages
    asyncio.run(run())


def test_scope_cleanup_closes_only_auxiliary_pages_and_preserves_borrowed_primary(tmp_path):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        async with instance.operation_scope('session-a', None):
            primary = await instance._new_page()
            primary.url = 'https://chatgpt.com/c/a'
            auxiliary = await instance._new_page()
            await instance.close_owned_pages(keep_for_user=True, retain_key='session-a')
        assert not primary.is_closed() and auxiliary.is_closed()
        async with instance.operation_scope('session-a', primary.url):
            borrowed, temporary = await instance._auxiliary_conversation_page(primary.url)
            assert borrowed is primary and temporary is False
            await instance._close_auxiliary_page(borrowed, temporary)
            await instance.close_owned_pages()
        assert not primary.is_closed()
        assert primary.draft == 'unsent user draft'
        primary.goto.assert_not_awaited()
        async with instance._resources.locked() as conn:
            assert conn.execute('SELECT retained,retain_key FROM pages').fetchall() == [(1, 'session-a')]
        await instance.close_retained_pages('session-a')
    asyncio.run(run())


def test_unnamed_manual_handoff_can_resume_and_releases_tab_after_completion(tmp_path):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        async with instance.operation_scope(None, None):
            page = await instance._new_page()
            page.url = 'https://chatgpt.com/c/manual'
            await instance.close_owned_pages(keep_for_user=True, retain_key='cgo_manual-task')
        assert not page.is_closed()
        async with instance.operation_scope(None, page.url):
            assert await instance._conversation_page(page.url) is page
            await instance.close_owned_pages()
        assert page.is_closed()
        async with instance._resources.locked() as conn:
            assert conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 0
    asyncio.run(run())


def test_full_profile_auxiliary_borrows_matching_session_and_rejects_other_session(tmp_path):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        pages = []
        for index in range(4):
            async with instance.operation_scope(f'session-{index}', None):
                page = await instance._new_page()
                page.url = f'https://chatgpt.com/c/{index}'
                pages.append(page)
                await instance.close_owned_pages(keep_for_user=True, retain_key=f'session-{index}')
        async with instance.operation_scope('session-0', pages[0].url):
            assert await instance._auxiliary_conversation_page(pages[0].url) == (pages[0], False)
            with pytest.raises(BrowserCapacityError, match='另一个 Session'):
                await instance._auxiliary_conversation_page(pages[1].url)
            assert await instance.close_conversation_tabs(pages[1].url) is False
            await instance.close_owned_pages()
        async with instance.operation_scope(None, None):
            with pytest.raises(BrowserCapacityError):
                await instance._page()
        assert context.peak == len(context.pages) == 4
        assert all(page.draft == 'unsent user draft' for page in pages)
        for index in range(4):
            await instance.close_retained_pages(f'session-{index}')
    asyncio.run(run())


def test_resident_export_keeps_draft_and_does_not_claim_unobserved_api_completeness(tmp_path):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        async with instance.operation_scope('session-a', None):
            page = await instance._new_page()
            page.url = 'https://chatgpt.com/c/a'
            await instance.close_owned_pages(keep_for_user=True, retain_key='session-a')
        instance._resident_conversation_request = AsyncMock(return_value=None)
        instance._page_requires_login = AsyncMock(return_value=False)
        instance._page_has_composer = AsyncMock(return_value=True)
        instance._load_all_rendered_messages = AsyncMock(return_value={'stable': True})
        instance._extract_dom_conversation = AsyncMock(return_value={'messages': [{'role': 'assistant', 'text': 'answer'}]})
        async with instance.operation_scope('session-a', page.url):
            result = await instance.export_conversation(page.url, scope='all')
            await instance.close_owned_pages()
        assert result['messages'][0]['text'] == 'answer'
        assert result['completeness']['overall'] == 'unverified'
        assert result['reason']['messages'] == 'api_response_unavailable'
        assert page.draft == 'unsent user draft'
        page.goto.assert_not_awaited()
        assert not page.is_closed() and context.peak == 1
        await instance.close_retained_pages('session-a')
    asyncio.run(run())


@pytest.mark.parametrize('interpreter', [False, True])
def test_resident_attachment_download_keeps_primary_page_and_draft(tmp_path, monkeypatch, interpreter):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        async with instance.operation_scope('session-a', None):
            page = await instance._new_page()
            page.url = 'https://chatgpt.com/c/a'
            await instance.close_owned_pages(keep_for_user=True, retain_key='session-a')
        instance._resident_conversation_request = AsyncMock(return_value=None)
        metadata = {'status': 200, 'payload': {'status': 'success', 'download_url': '/backend-api/estuary/content?id=file-1', 'metadata': {'file_id': 'file-1'}}}
        monkeypatch.setattr('chatgpt_oracle.browser._authenticated_page_json_get', AsyncMock(return_value=metadata))
        context.request = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(ok=True, body=AsyncMock(return_value=b'complete bytes'), headers={})))
        source = (
            'https://chatgpt.com/backend-api/conversation/a/interpreter/download?message_id=msg-1&sandbox_path=%2Fmnt%2Fdata%2Fproof.txt'
            if interpreter else 'https://chatgpt.com/backend-api/files/download/file-1?conversation_id=a'
        )
        async with instance.operation_scope('session-a', page.url):
            result = await instance.download_attachment(source, attachment_id='file-1', conversation_url=page.url)
            await instance.close_owned_pages()
        assert result[0] == b'complete bytes'
        assert page.draft == 'unsent user draft'
        page.goto.assert_not_awaited()
        assert not page.is_closed() and context.peak == 1
        await instance.close_retained_pages('session-a')
    asyncio.run(run())


def test_ui_mutex_is_shared_and_does_not_cover_independent_page_reads(tmp_path):
    async def run():
        context = Context()
        first, second = browser(tmp_path, context), browser(tmp_path, context)
        entered = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def owner():
            async with first._resources.ui_locked():
                async with first._resources.ui_locked():
                    order.append('first')
                    entered.set()
                    await release.wait()

        async def follower():
            await entered.wait()
            async with second._resources.ui_locked():
                order.append('second')

        owner_task = asyncio.create_task(owner())
        follower_task = asyncio.create_task(follower())
        await entered.wait()
        # The allocation database remains readable while another process owns
        # the UI transaction; model polling never acquires this UI mutex.
        async with second._resources.locked() as conn:
            assert conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 0
        assert order == ['first']
        release.set()
        await asyncio.wait_for(asyncio.gather(owner_task, follower_task), 3)
        assert order == ['first', 'second']
    asyncio.run(run())


def test_cdp_failure_does_not_launch_an_alternative_profile(tmp_path, monkeypatch):
    async def run():
        instance = ChatGPTBrowser(replace(Settings.from_env(tmp_path),browser_profile_dir=tmp_path/'profile',browser_launch_cdp=False))
        driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock()),stop=AsyncMock())
        monkeypatch.setattr('chatgpt_oracle.browser.async_playwright',lambda:SimpleNamespace(start=AsyncMock(return_value=driver)))
        instance._connect_cdp = AsyncMock(side_effect=RuntimeError('endpoint unavailable'))
        with pytest.raises(BrowserTaskError,match='不会另外启动'):
            await instance.start()
        driver.chromium.launch_persistent_context.assert_not_awaited()
        driver.stop.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('outcome,ready,message',[
    (None,True,None),
    (BrowserNeedsUserAction('login_required','login'),False,'登录'),
    (BrowserTaskError('loading'),False,'尚未就绪'),
])
def test_cold_status_reports_hydrated_state_not_loading_placeholder(tmp_path,outcome,ready,message):
    async def run():
        instance=ChatGPTBrowser(Settings.from_env(tmp_path))
        page=SimpleNamespace(url='https://chatgpt.com/',title=AsyncMock(return_value='ChatGPT'))
        instance._page=AsyncMock(return_value=page)
        instance._page_requires_login=AsyncMock(return_value=False)
        instance._page_has_composer=AsyncMock(return_value=False)
        instance._check_account_page_error=AsyncMock(return_value=None)
        instance._require_composer=AsyncMock(side_effect=outcome)
        result=await instance.browser_status()
        assert result['ready'] is ready and result['logged_in'] is ready
        if message:assert message in result['message']
        else:assert result['message'] is None
    asyncio.run(run())



def test_other_worker_reaps_dead_auxiliary_but_session_recovers_original_target(tmp_path):
    async def run():
        context = Context()
        worker_a = browser(tmp_path, context)
        async with worker_a.operation_scope('session-a', None):
            primary = await worker_a._new_page()
            primary.url = 'https://chatgpt.com/c/a'
            auxiliary = await worker_a._new_page()
            async with worker_a._resources.locked() as conn:
                conn.execute('UPDATE pages SET owner_pid=0 WHERE session_key=?', ('session-a',))
        worker_b = browser(tmp_path, context)
        async with worker_b.operation_scope('session-b', None):
            second = await worker_b._new_page()
            second.url = 'https://chatgpt.com/c/b'
            assert not primary.is_closed()
            assert auxiliary.is_closed()
            assert len(context.pages) == 2
            async with worker_b._resources.locked() as conn:
                assert conn.execute('SELECT owner_pid,retained,session_primary FROM pages WHERE target_id=?', (primary.target,)).fetchone() == (None, 1, 1)
            recovered_a = browser(tmp_path, context)
            origin = primary.time_origin
            async with recovered_a.operation_scope('session-a', primary.url):
                assert await recovered_a._conversation_page(primary.url) is primary
                assert primary.time_origin is origin
                primary.goto.assert_not_awaited()
                await recovered_a.close_owned_pages(keep_for_user=True, retain_key='session-a')
            await worker_b.close_owned_pages()
            await recovered_a.close_retained_pages('session-a')
        assert not context.pages
    asyncio.run(run())


@pytest.mark.parametrize('home_state', ['empty', 'draft', 'attachment', 'unready'])
def test_home_page_reuse_requires_a_verified_empty_composer(tmp_path, home_state):
    async def run():
        context = Context()
        home = await context.new_page()
        home.url = 'https://chatgpt.com/'
        home.draft = 'keep this draft' if home_state == 'draft' else ''
        home.evaluate = AsyncMock(return_value=home_state == 'empty')
        if home_state == 'unready':
            home.evaluate.side_effect = PlaywrightError('navigation in progress')
        instance = browser(tmp_path, context)
        async with instance.operation_scope('session-a', None):
            page = await instance._new_page()
            assert (page is home) is (home_state == 'empty')
            home.evaluate.assert_awaited_once()
            assert home.draft == ('keep this draft' if home_state == 'draft' else '')
            home.goto.assert_not_awaited()
            await instance.close_owned_pages()
        assert home.is_closed() is (home_state == 'empty')
    asyncio.run(run())


@pytest.mark.parametrize('retain_session', [False, True])
def test_first_named_export_leaves_primary_for_outer_session_cleanup(tmp_path, retain_session):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        url = 'https://chatgpt.com/c/export-only'
        async with instance.operation_scope('session-export', url):
            primary, temporary = await instance._auxiliary_conversation_page(url)
            assert temporary is True
            primary.url = url
            await instance._close_auxiliary_page(primary, temporary)
            assert not primary.is_closed()
            await instance.close_owned_pages(keep_for_user=retain_session, retain_key='session-export')
        assert primary.is_closed() is not retain_session
        if retain_session:
            async with instance.operation_scope('session-export', url):
                assert await instance._conversation_page(url) is primary
                primary.goto.assert_not_awaited()
                await instance.close_owned_pages(keep_for_user=True, retain_key='session-export')
            await instance.close_retained_pages('session-export')
        assert not context.pages
    asyncio.run(run())


def test_export_secondary_page_closes_even_when_primary_is_adopted_in_new_scope(tmp_path):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        async with instance.operation_scope('session-a', None):
            primary = await instance._new_page()
            primary.url = 'https://chatgpt.com/c/a'
            await instance.close_owned_pages(keep_for_user=True, retain_key='session-a')
        async with instance.operation_scope('session-a', primary.url):
            auxiliary, temporary = await instance._auxiliary_conversation_page('https://chatgpt.com/c/other-export')
            assert auxiliary is not primary and temporary is True
            await instance._close_auxiliary_page(auxiliary, temporary)
            assert auxiliary.is_closed() and not primary.is_closed()
            await instance.close_owned_pages(keep_for_user=True, retain_key='session-a')
        assert context.pages == [primary]
        await instance.close_retained_pages('session-a')
    asyncio.run(run())



def test_resource_registry_migration_preserves_named_primary_after_worker_death(tmp_path):
    async def run():
        context = Context()
        primary, auxiliary = [await context.new_page() for _ in range(2)]
        primary.url = 'https://chatgpt.com/c/legacy-session'
        instance = browser(tmp_path, context)
        conn = sqlite3.connect(instance._resources.path)
        try:
            conn.execute('CREATE TABLE pages(target_id TEXT PRIMARY KEY,owner_pid INTEGER,retained INTEGER NOT NULL DEFAULT 0,retain_key TEXT,session_key TEXT)')
            conn.executemany('INSERT INTO pages VALUES(?,0,0,?,?)', [
                (primary.target, 'legacy-session', 'legacy-session'),
                (auxiliary.target, 'legacy-session', 'legacy-session'),
            ])
            conn.commit()
        finally:
            conn.close()
        async with instance.operation_scope('new-session', None):
            await instance._new_page()
            assert not primary.is_closed() and auxiliary.is_closed()
            await instance.close_owned_pages()
        async with instance.operation_scope('legacy-session', primary.url):
            assert await instance._conversation_page(primary.url) is primary
            await instance.close_owned_pages(keep_for_user=True, retain_key='legacy-session')
        await instance.close_retained_pages('legacy-session')
        assert not context.pages
    asyncio.run(run())


def test_last_cdp_page_becomes_unowned_blank_without_terminating_chrome(tmp_path):
    async def run():
        context = Context()
        instance = browser(tmp_path, context)
        async with instance.operation_scope('last-session'):
            page = await instance._new_page()
            page.url = 'https://chatgpt.com/c/last'
            await instance.close_owned_pages(keep_for_user=True, retain_key='last-session')
        instance._connected_cdp = True
        cdp = SimpleNamespace(send=AsyncMock(return_value={'targetInfos': [{'type': 'page', 'targetId': page.target}]}), detach=AsyncMock())
        instance._browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))
        async def navigate(url, **kwargs):
            page.url = url
        page.goto.side_effect = navigate
        async with instance.operation_scope('last-session'):
            await instance.close_conversation_tabs(page.url)
        assert context.pages == [page] and not page.is_closed() and page.url == 'about:blank'
        async with instance._resources.locked() as conn:
            assert conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 0
        async with instance.operation_scope('next-session'):
            assert await instance._new_page() is page
            await instance.close_owned_pages()
        assert context.peak == 1
    asyncio.run(run())
