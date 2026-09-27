from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from chatgpt_oracle.browser import BrowserSnapshot, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager


class SessionBrowser:
    def __init__(self) -> None:
        self.submits: list[tuple[str, bool]] = []
        self.continues: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.tabs: set[str] = set()

    async def submit(self, mode: str, prompt: str, **kwargs: object) -> SubmitReceipt:
        dedicated = bool(kwargs.get("dedicated_page"))
        self.submits.append((prompt, dedicated))
        url = "https://chatgpt.com/c/session-test"
        self.tabs.add(url)
        return SubmitReceipt(url, 0)

    async def continue_conversation(self, url: str, prompt: str, **kwargs: object) -> SubmitReceipt:
        self.continues.append((url, prompt))
        self.tabs.add(url)
        return SubmitReceipt(url, 0)

    async def inspect(self, receipt: SubmitReceipt) -> BrowserSnapshot:
        return BrowserSnapshot("COMPLETE", receipt.conversation_url, result_text="ok")

    async def has_conversation_tab(self, url: str) -> bool:
        return url in self.tabs

    async def close_conversation_tabs(self, url: str) -> bool:
        self.tabs.discard(url)
        self.closed.append(url)
        return True

    async def close(self) -> None:
        return None


def test_named_session_binds_dedicated_tab_and_resumes(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            browser_profile_dir=tmp_path / "profile",
            sessions_dir=tmp_path / "sessions",
            consult_poll_seconds=0.01,
            consult_timeout_seconds=1,
        )
        browser = SessionBrowser()
        manager = JobManager(settings, browser)
        first = await manager.submit("consult", "first", session_id="agent-a")
        for _ in range(100):
            if manager.get(first.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        session = manager.sessions.get("agent-a")
        assert session and session.conversation_url
        assert browser.submits == [("first", True)]
        resumed = await manager.resume_session("agent-a", "second")
        for _ in range(100):
            if manager.get(resumed.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        assert browser.continues == [(session.conversation_url, "second")]
        assert manager.get(resumed.id).session_id == "agent-a"
        assert manager.sessions.get("agent-a").state == "LIVE"
        await manager.stop()

    asyncio.run(run())


def test_expired_session_closes_only_its_tab(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(Settings.from_env(tmp_path), sessions_dir=tmp_path / "sessions")
        browser = SessionBrowser()
        manager = JobManager(settings, browser)
        session = manager.sessions.create("agent-a")
        session.conversation_url = "https://chatgpt.com/c/session-test"
        session.expires_at = "2000-01-01T00:00:00+00:00"
        manager.sessions.save(session)
        await manager._expire_sessions()
        current = manager.sessions.get("agent-a")
        assert current and current.state == "DEAD"
        assert browser.closed == [session.conversation_url]
        await manager.stop()

    asyncio.run(run())
