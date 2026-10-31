import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatgpt_oracle.browser import BrowserSnapshot, SubmitReceipt, ChatGPTBrowser
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.scheduler import Scheduler


def schedulers(tmp_path):
    path = tmp_path / "pool.sqlite3"
    return (Scheduler(tmp_path / "gpt", shared_pool_path=path, namespace="chatgpt"),
            Scheduler(tmp_path / "gem", shared_pool_path=path, namespace="gemini"))


def enqueue(scheduler, task, session, request=None):
    scheduler.enqueue(task, "consult", on_busy="queue", retry_after_seconds=1,
                      session_id=session, request_id=request, request_hash=task)
    scheduler.set_state(task, "QUEUED")


def test_provider_settings_share_profile_cdp_and_pool_without_sharing_data(monkeypatch, tmp_path):
    monkeypatch.setenv("ORACLE_SHARED_BROWSER", "1")
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path / "shared-profile"))
    monkeypatch.setenv("ORACLE_BROWSER_CDP_URL", "http://127.0.0.1:9333")
    monkeypatch.setenv("CHATGPT_PROFILE_DIR", str(tmp_path / "old-gpt"))
    monkeypatch.setenv("GEMINI_PROFILE_DIR", str(tmp_path / "old-gem"))
    monkeypatch.setenv("CHATGPT_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    a = Settings.from_env(tmp_path, provider="chatgpt")
    b = Settings.from_env(tmp_path, provider="gemini")
    assert a.shared_browser and b.shared_browser
    assert a.browser_profile_dir == b.browser_profile_dir == tmp_path / "shared-profile"
    assert a.browser_cdp_url == b.browser_cdp_url == "http://127.0.0.1:9333"
    assert a.browser_pool_path == b.browser_pool_path
    assert a.jobs_dir != b.jobs_dir and a.sessions_dir != b.sessions_dir


def test_shared_user_settings_work_in_a_shell_created_before_deployment(monkeypatch, tmp_path):
    import chatgpt_oracle.config as config
    values = {"ORACLE_SHARED_BROWSER": "1", "ORACLE_BROWSER_PROFILE_DIR": str(tmp_path / "existing-native"),
              "ORACLE_BROWSER_CDP_URL": "http://127.0.0.1:9333", "ORACLE_BROWSER_LAUNCH_CDP": "1"}
    for name in values:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_shared_browser_user_value", values.get)
    a = Settings.from_env(tmp_path, provider="chatgpt")
    b = Settings.from_env(tmp_path, provider="gemini")
    assert a.shared_browser and b.shared_browser
    assert a.browser_profile_dir == b.browser_profile_dir == tmp_path / "existing-native"
    assert a.browser_cdp_url == b.browser_cdp_url == "http://127.0.0.1:9333"


def test_same_session_and_request_ids_are_separate_across_providers(tmp_path):
    a, b = schedulers(tmp_path)
    enqueue(a, "gpt-task", "same", "same-request")
    enqueue(b, "gem-task", "same", "same-request")
    assert a.reserve_next(os.getpid(), "g") == "gpt-task"
    assert b.reserve_next(os.getpid(), "b") == "gem-task"
    assert a.operation_session("gpt-task") == b.operation_session("gem-task") == "same"
    assert a.idempotent_task("same-request", "gpt-task") == "gpt-task"
    assert b.idempotent_task("same-request", "gem-task") == "gem-task"
    a.complete("gpt-task", "COMPLETE")
    b.complete("gem-task", "COMPLETE")
    assert a.status()["session_count"] == b.status()["session_count"] == 2
    assert a.release_session("same")
    assert b.status()["session_count"] == 1
    assert b.status()["session_slots"][0]["session_key"] == "same"


def test_combined_four_slots_keep_resident_continuation_and_release_fifth(tmp_path):
    a, b = schedulers(tmp_path)
    for scheduler, task, session in [(a, "a1", "a1"), (a, "a2", "a2"), (b, "b1", "b1"), (b, "b2", "b2")]:
        enqueue(scheduler, task, session)
        assert scheduler.reserve_next(os.getpid(), task) == task
        scheduler.complete(task, "COMPLETE")
    enqueue(a, "fifth", "fifth")
    assert a.reserve_next(os.getpid(), "five") is None
    enqueue(b, "follow", "b1")
    assert b.reserve_next(os.getpid(), "follow") == "follow"
    b.complete("follow", "COMPLETE")
    assert b.release_session("b1")
    assert a.reserve_next(os.getpid(), "five") == "fifth"
    assert a.status()["session_count"] == b.status()["session_count"] == 4


def test_concurrent_provider_claims_never_overbook_four_slots(tmp_path):
    a, b = schedulers(tmp_path)
    for index in range(4):
        enqueue(a, f"a{index}", f"a{index}")
        enqueue(b, f"b{index}", f"b{index}")
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda i: (a if i % 2 == 0 else b).reserve_next(os.getpid(), str(i)), range(8)))
    assert sum(value is not None for value in results) == 4
    assert a.status()["session_count"] == b.status()["session_count"] == 4


def test_private_workers_and_recovery_do_not_consume_foreign_jobs(tmp_path):
    a, b = schedulers(tmp_path)
    enqueue(b, "gem-only", "same")
    assert a.reserve_next(os.getpid(), "a") is None
    assert b.reserve_next(99999999, "dead") == "gem-only"
    class EmptyStore:
        def get(self, task):
            raise AssertionError("Foreign jobs must not be resolved through the other provider store")
    a.recover_startup(EmptyStore())
    assert b.status("gem-only")["scheduler_state"] == "STARTING"


def test_idle_legacy_lane_migrates_once_and_preserves_idempotency(tmp_path):
    local = tmp_path / "gpt"
    old = Scheduler(local)
    enqueue(old, "original", "session", "request")
    assert old.reserve_next(os.getpid(), "old") == "original"
    old.complete("original", "COMPLETE")
    shared = Scheduler(local, shared_pool_path=tmp_path / "pool.sqlite3", namespace="chatgpt")
    assert shared.status()["session_slots"][0]["session_key"] == "session"
    assert shared.idempotent_task("request", "original") == "original"
    again = Scheduler(local, shared_pool_path=tmp_path / "pool.sqlite3", namespace="chatgpt")
    assert again.status()["session_count"] == 1
    enqueue(again, "next", "session")
    assert again.reserve_next(os.getpid(), "new") == "next"
    assert again.operation_session("next") == "session"


class SharedFakeBrowser:
    def __init__(self, provider, pages):
        self.provider = provider
        self.pages = pages
        self.current = None
        self.observed_scopes = []

    @asynccontextmanager
    async def operation_scope(self, session_id=None, conversation_url=None):
        self.observed_scopes.append(session_id)
        previous = self.current
        self.current = session_id
        try:
            yield
        finally:
            self.current = previous

    async def submit(self, mode, prompt, **kwargs):
        origin = "https://chatgpt.com/c/" if self.provider == "chatgpt" else "https://gemini.google.com/app/"
        url = origin + prompt
        self.pages[self.current] = url
        return SubmitReceipt(url, 0)

    async def continue_conversation(self, url, prompt, **kwargs):
        return SubmitReceipt(url, 0)

    async def inspect(self, receipt):
        return BrowserSnapshot("COMPLETE", receipt.conversation_url, result_text="done")

    async def close_owned_pages(self, **kwargs):
        pass

    async def has_conversation_tab(self, url):
        return url in self.pages.values()

    async def has_session_page(self, session):
        return session in self.pages

    async def close_conversation_tabs(self, url):
        for key in [key for key, value in self.pages.items() if value == url]:
            del self.pages[key]

    async def close_retained_pages(self, key):
        self.pages.pop(key, None)

    async def close(self):
        pass


def test_managers_share_capacity_and_keep_same_named_contexts_separate(monkeypatch, tmp_path):
    monkeypatch.setenv("ORACLE_SHARED_BROWSER", "1")
    pages = {}
    browsers = [SharedFakeBrowser(provider, pages) for provider in ("chatgpt", "gemini")]
    settings = [replace(Settings.from_env(tmp_path, provider=provider), consult_poll_seconds=.01)
                for provider in ("chatgpt", "gemini")]
    managers = [JobManager(value, browser) for value, browser in zip(settings, browsers)]
    async def wait(manager, task):
        async with asyncio.timeout(4):
            while manager.get(task.id).state != "COMPLETE" or manager.scheduler.session_active(task.session_id):
                assert manager.get(task.id).state not in {"ERROR", "CANCELLED"}
                await asyncio.sleep(.01)
    async def run():
        a, b = managers
        try:
            for manager, session in [(a, "same"), (a, "a2"), (b, "same"), (b, "b2")]:
                task = await manager.submit("consult", session, session_id=session)
                await wait(manager, task)
            assert len(pages) == 4 and "chatgpt:same" in pages and "gemini:same" in pages
            fifth = await a.submit("consult", "fifth", session_id="fifth")
            await asyncio.sleep(.05)
            assert a.get(fifth.id).state == "QUEUED"
            follow = await b.resume_session("same", "follow")
            await wait(b, follow)
            await a.session_close("same")
            assert "gemini:same" in pages
            await wait(a, fifth)
            assert len(pages) == 4
            assert all(scope.startswith("chatgpt:") for scope in browsers[0].observed_scopes if scope)
            assert all(scope.startswith("gemini:") for scope in browsers[1].observed_scopes if scope)
        finally:
            for manager in managers:
                for session in manager.sessions.all():
                    if session.state == "LIVE":
                        await manager.session_close(session.id)
                await manager.stop()
    asyncio.run(run())


def test_shared_adapter_disconnect_does_not_close_native_browser(monkeypatch, tmp_path):
    from chatgpt_oracle.gemini_browser import GeminiBrowser
    monkeypatch.setenv("ORACLE_SHARED_BROWSER", "1")
    settings = Settings.from_env(tmp_path)
    async def run():
        native = SimpleNamespace(close=AsyncMock())
        driver = SimpleNamespace(stop=AsyncMock())
        connector = ChatGPTBrowser(settings)
        connector._browser = native
        connector._context = object()
        connector._connected_cdp = True
        connector._playwright = driver
        connector.close_owned_pages = AsyncMock()
        gemini = GeminiBrowser(replace(settings, provider="gemini"))
        gemini._native_connector = connector
        gemini._browser = native
        gemini._context = connector._context
        gemini._playwright = driver
        await gemini.close()
        native.close.assert_not_awaited()
        driver.stop.assert_awaited_once()
    asyncio.run(run())
