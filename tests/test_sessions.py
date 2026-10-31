from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job, TERMINAL_STATES
from chatgpt_oracle.uploads import InMemoryUpload


class SessionBrowser:
    def __init__(self) -> None:
        self.submits: list[tuple[str, bool]] = []
        self.continues: list[tuple[str, str]] = []
        self.continue_selections: list[dict[str, Any]] = []
        self.closed: list[str] = []
        self.tabs: set[str] = set()

    async def submit(self, mode: str, prompt: str, **kwargs: object) -> SubmitReceipt:
        dedicated = bool(kwargs.get("dedicated_page"))
        self.submits.append((prompt, dedicated))
        url = "https://chatgpt.com/c/session-test"
        self.tabs.add(url)
        return SubmitReceipt(url, 0)

    async def continue_conversation(
        self,
        url: str,
        prompt: str,
        *,
        model: str | None = None,
        quality: str = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        attachments: list[InMemoryUpload] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    ) -> SubmitReceipt:
        self.continues.append((url, prompt))
        self.continue_selections.append({
            "model": model,
            "quality": quality,
            "reasoning_effort": reasoning_effort,
            "allow_downgrade": allow_downgrade,
            "attachment_bytes": [item.data for item in attachments or []],
        })
        self.tabs.add(url)
        metadata = [
            {**item.metadata.public(), "verified": True}
            for item in attachments or []
        ]
        if metadata and on_upload_verified:
            await on_upload_verified(metadata)
        return SubmitReceipt(
            url,
            2,
            selected_model=model,
            requested_quality=quality,
            requested_reasoning_effort=reasoning_effort,
            resolved_model=model or "current-model",
            resolved_reasoning_effort=reasoning_effort or quality,
            selection_source="session-test",
            selection_verified_at="2026-10-02T00:00:00+00:00",
            attachments=metadata,
        )

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
        assert browser.submits == [(manager.get(first.id).sending_text, True)]
        assert manager.get(first.id).prompt == "first"
        resumed = await manager.resume_session("agent-a", "second")
        for _ in range(100):
            if manager.get(resumed.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        assert browser.continues == [(session.conversation_url, manager.get(resumed.id).sending_text)]
        assert manager.get(resumed.id).prompt == "second"
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


def session_settings(tmp_path: Path) -> Settings:
    return replace(
        Settings.from_env(tmp_path),
        jobs_dir=tmp_path / "jobs",
        browser_profile_dir=tmp_path / "profile",
        sessions_dir=tmp_path / "sessions",
        consult_poll_seconds=0.01,
        research_poll_seconds=0.01,
        consult_timeout_seconds=1,
        research_timeout_seconds=1,
    )


async def wait_terminal(manager: JobManager, task_id: str) -> Job:
    for _ in range(200):
        current = manager.get(task_id)
        if current.state in TERMINAL_STATES:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not finish: {manager.get(task_id).state}")


@pytest.mark.parametrize("override", [False, True])
def test_resume_applies_inherited_or_overridden_selection(tmp_path: Path, override: bool) -> None:
    async def run() -> None:
        browser = SessionBrowser()
        manager = JobManager(session_settings(tmp_path), browser)
        parent = Job.create(
            "consult", "first", session_id="selection-session", model="parent-model",
            quality="pro", reasoning_effort="long", allow_downgrade=True,
        )
        parent.state = "COMPLETE"
        parent.conversation_url = "https://chatgpt.com/c/selection-session"
        manager.store.save(parent)
        manager.sessions.touch(
            parent.session_id, conversation_url=parent.conversation_url, last_task_id=parent.id,
        )
        expected_selection = {
            "model": "replacement-model" if override else "parent-model",
            "quality": "extra_high" if override else "pro",
            "reasoning_effort": "extended" if override else "long",
            "allow_downgrade": not override,
        }
        try:
            job = await manager.resume_session(
                parent.session_id, "second", **(expected_selection if override else {}),
            )
            current = await wait_terminal(manager, job.id)
            assert current.state == "COMPLETE"
            assert browser.submits == []
            assert browser.continues == [(parent.conversation_url, current.sending_text)]
            assert current.prompt == "second" and current.sending_text.startswith("【CODEX_ORACLE】\n")
            assert browser.continue_selections == [{**expected_selection, "attachment_bytes": []}]
            assert current.resolved_model == expected_selection["model"]
            assert current.resolved_reasoning_effort == expected_selection["reasoning_effort"]
            assert current.selection_source == "session-test"
            assert current.selection_verified_at == "2026-10-02T00:00:00+00:00"
        finally:
            await manager.stop()

    asyncio.run(run())


@pytest.mark.parametrize("last_task_id", [None, "cgo_pruned"])
@pytest.mark.parametrize("with_upload", [False, True])
def test_resume_without_parent_task_keeps_conversation(
    tmp_path: Path, last_task_id: str | None, with_upload: bool,
) -> None:
    async def run() -> None:
        browser = SessionBrowser()
        manager = JobManager(session_settings(tmp_path), browser)
        conversation_url = "https://chatgpt.com/c/preserved-session"
        session = manager.sessions.touch(
            "preserved-session", conversation_url=conversation_url, last_task_id=last_task_id,
        )
        upload_ids = []
        if with_upload:
            upload = await manager.uploads.put("source.txt", "text/plain", b"session source")
            upload_ids.append(upload.upload_id)
        try:
            job = await manager.resume_session(
                session.id, "continue preserved session", mode="research", upload_ids=upload_ids,
            )
            current = await wait_terminal(manager, job.id)
            assert current.state == "COMPLETE"
            assert current.parent_id is None
            assert current.conversation_url == conversation_url
            assert current.quality == "auto"
            assert current.resolved_reasoning_effort == "pro"
            assert browser.submits == []
            assert browser.continues == [(conversation_url, current.sending_text)]
            assert current.prompt == "continue preserved session"
            assert browser.continue_selections == [{
                "model": None,
                "quality": "pro",
                "reasoning_effort": None,
                "allow_downgrade": False,
                "attachment_bytes": [b"session source"] if with_upload else [],
            }]
            assert bool(current.attachments) is with_upload
            updated_session = manager.sessions.get(session.id)
            assert updated_session is not None
            assert updated_session.conversation_url == conversation_url
            assert updated_session.last_task_id == current.id
        finally:
            await manager.stop()

    asyncio.run(run())
