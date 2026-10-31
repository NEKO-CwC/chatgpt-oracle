from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, ModelUnavailableError, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job, TERMINAL_STATES
from chatgpt_oracle.uploads import InMemoryUpload


class FakeBrowser:
    def __init__(self, snapshots: list[BrowserSnapshot]):
        self.snapshots = list(snapshots)
        self.submitted: list[str] = []
        self.qualities: list[str] = []
        self.continued: list[dict[str, Any]] = []
        self.raise_model_unavailable = False

    async def submit(
        self,
        mode: str,
        prompt: str,
        *,
        model: str | None = None,
        quality: str = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
    ) -> SubmitReceipt:
        if self.raise_model_unavailable:
            raise ModelUnavailableError("pro", ["极高"])
        self.submitted.append(prompt)
        self.qualities.append(quality)
        return SubmitReceipt(
            "https://chatgpt.com/c/test",
            0,
            model,
            requested_quality=quality,
            requested_reasoning_effort=reasoning_effort,
            resolved_reasoning_effort=quality if quality != "auto" else None,
            selection_source="test",
            current_user_message_id="fallback-turn-0:0",
            submission_state="confirmed",
        )

    async def continue_conversation(
        self,
        conversation_url: str,
        prompt: str,
        *,
        model: str | None = None,
        quality: str = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        attachments: list[InMemoryUpload] | None = None,
        on_upload_verified: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    ) -> SubmitReceipt:
        if self.raise_model_unavailable:
            raise ModelUnavailableError("pro", ["极高"])
        self.submitted.append(prompt)
        self.continued.append({
            "conversation_url": conversation_url,
            "model": model,
            "quality": quality,
            "reasoning_effort": reasoning_effort,
            "allow_downgrade": allow_downgrade,
            "attachment_bytes": [item.data for item in attachments or []],
        })
        metadata = [
            {**item.metadata.public(), "verified": True}
            for item in attachments or []
        ]
        if metadata and on_upload_verified:
            await on_upload_verified(metadata)
        return SubmitReceipt(
            conversation_url,
            3,
            selected_model=model,
            requested_quality=quality,
            requested_reasoning_effort=reasoning_effort,
            resolved_model=model or "current-model",
            resolved_reasoning_effort=reasoning_effort or quality,
            selection_source="test",
            selection_verified_at="2026-10-02T00:00:00+00:00",
            attachments=metadata,
        )

    async def inspect(self, receipt: SubmitReceipt) -> BrowserSnapshot:
        if self.snapshots:
            return self.snapshots.pop(0)
        return BrowserSnapshot("RUNNING", receipt.conversation_url)

    async def close(self) -> None:
        return None


def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        jobs_dir=tmp_path / "jobs",
        browser_profile_dir=tmp_path / "profile",
        chrome_executable=None,
        browser_headless=True,
        browser_start_minimized=False,
        browser_timeout_seconds=1,
        consult_poll_seconds=0.01,
        critic_poll_seconds=0.01,
        research_poll_seconds=0.01,
        consult_timeout_seconds=1,
        critic_timeout_seconds=1,
        research_timeout_seconds=1,
        host="127.0.0.1",
        port=8787,
        transport="stdio",
        mcp_path="/mcp",
        bearer_token=None,
        allow_insecure_remote=False,
    )


def test_manager_completes_and_persists_result(tmp_path: Path) -> None:
    async def run() -> None:
        browser = FakeBrowser([
            BrowserSnapshot("COMPLETE", "https://chatgpt.com/c/test", result_text="answer", sources=["https://example.com"])
        ])
        manager = JobManager(settings(tmp_path), browser)
        job = await manager.submit("consult", "What is the answer?")
        for _ in range(100):
            if manager.get(job.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        result_job, result = manager.result(job.id)
        assert result_job.state == "COMPLETE"
        assert "answer" in result
        assert result_job.sources == ["https://example.com"]
        await manager.stop()

    asyncio.run(run())


def test_manager_exposes_manual_action(tmp_path: Path) -> None:
    async def run() -> None:
        browser = FakeBrowser([
            BrowserSnapshot("NEEDS_USER_INPUT", "https://chatgpt.com/c/test", user_action="Login required")
        ])
        manager = JobManager(settings(tmp_path), browser)
        job = await manager.submit("research", "Research this")
        for _ in range(100):
            if manager.get(job.id).state == "NEEDS_USER_INPUT":
                break
            await asyncio.sleep(0.01)
        assert manager.get(job.id).user_action == "Login required"
        await manager.stop()

    asyncio.run(run())


def test_research_waits_for_stable_report(tmp_path: Path) -> None:
    async def run() -> None:
        browser = FakeBrowser([
            BrowserSnapshot("COMPLETE", "https://chatgpt.com/c/test", result_text="partial"),
            BrowserSnapshot("COMPLETE", "https://chatgpt.com/c/test", result_text="full report"),
            BrowserSnapshot("COMPLETE", "https://chatgpt.com/c/test", result_text="full report"),
        ])
        manager = JobManager(settings(tmp_path), browser)
        job = await manager.submit("research", "Research this")
        for _ in range(100):
            if manager.get(job.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        _, result = manager.result(job.id)
        assert result is not None
        assert "full report" in result
        assert "partial" not in result
        await manager.stop()

    asyncio.run(run())


def test_settled_turn_timeout_reports_idle_evidence(tmp_path: Path) -> None:
    async def run() -> None:
        # The page kept our turn but showed no generation signal at all: the
        # poller must count that as idle evidence, not as "still generating".
        browser = FakeBrowser([
            BrowserSnapshot("RUNNING", "https://chatgpt.com/c/test", generation_active=False)
            for _ in range(60)
        ])
        manager = JobManager(settings(tmp_path), browser)
        job = await manager.submit("critic", "Judge this")
        for _ in range(400):
            if manager.get(job.id).state == "TIMEOUT":
                break
            await asyncio.sleep(0.01)
        final = manager.get(job.id)
        assert final.error_code == "TURN_RESULT_WAIT_TIMEOUT"
        assert "网页仍显示当前回合进行中" not in final.error
        assert "无生成信号" in final.error
        milestones = json.loads(
            (tmp_path / "jobs" / "diagnostics" / f"{job.id}.json").read_text(encoding="utf-8")
        )["milestones"]
        assert milestones["turn_stream_stage"]["stage"] == "settled_idle"
        assert milestones["turn_stream_unsettled"]["settled_unchangeable"] is True
        await manager.stop()

    asyncio.run(run())


def test_generating_turn_timeout_keeps_generation_reason(tmp_path: Path) -> None:
    async def run() -> None:
        browser = FakeBrowser([
            BrowserSnapshot("RUNNING", "https://chatgpt.com/c/test", generation_active=True)
            for _ in range(60)
        ])
        manager = JobManager(settings(tmp_path), browser)
        job = await manager.submit("critic", "Judge this")
        for _ in range(400):
            if manager.get(job.id).state == "TIMEOUT":
                break
            await asyncio.sleep(0.01)
        final = manager.get(job.id)
        assert final.error_code == "GENERATION_WAIT_TIMEOUT"
        assert "网页仍显示当前回合进行中" in final.error
        await manager.stop()

    asyncio.run(run())


def test_default_quality_resolves_by_task_mode(tmp_path: Path) -> None:
    async def run() -> None:
        browser = FakeBrowser([
            BrowserSnapshot("COMPLETE", "https://chatgpt.com/c/test", result_text="answer"),
            BrowserSnapshot("COMPLETE", "https://chatgpt.com/c/test", result_text="research"),
            BrowserSnapshot("COMPLETE", "https://chatgpt.com/c/test", result_text="research"),
        ])
        manager = JobManager(settings(tmp_path), browser)
        consult = await manager.submit("consult", "quick")
        research = await manager.submit("research", "deep", on_busy="queue")
        for _ in range(100):
            if manager.get(consult.id).state == "COMPLETE" and manager.get(research.id).state == "COMPLETE":
                break
            await asyncio.sleep(0.01)
        assert browser.qualities == ["extra_high", "pro"]
        assert manager.get(consult.id).quality == "auto"
        assert manager.get(research.id).quality == "auto"
        await manager.stop()

    asyncio.run(run())


def test_model_unavailable_is_structured(tmp_path: Path) -> None:
    async def run() -> None:
        browser = FakeBrowser([])
        browser.raise_model_unavailable = True
        manager = JobManager(settings(tmp_path), browser)
        job = await manager.submit("consult", "needs pro", quality="pro")
        for _ in range(100):
            if manager.get(job.id).state == "MODEL_UNAVAILABLE":
                break
            await asyncio.sleep(0.01)
        current = manager.get(job.id)
        assert current.state == "MODEL_UNAVAILABLE"
        assert current.error_code == "MODEL_UNAVAILABLE"
        assert "极高" in (current.user_action or "")
        await manager.stop()

    asyncio.run(run())


async def wait_terminal(manager: JobManager, task_id: str) -> Job:
    for _ in range(200):
        job = manager.get(task_id)
        if job.state in TERMINAL_STATES:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not finish: {manager.get(task_id).state}")


@pytest.mark.parametrize("with_upload", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_continuation_applies_selection_and_persists_receipt(
    tmp_path: Path, with_upload: bool, override: bool,
) -> None:
    async def run() -> None:
        conversation_url = "https://chatgpt.com/c/test"
        browser = FakeBrowser([
            BrowserSnapshot("COMPLETE", conversation_url, result_text="continued answer"),
        ])
        manager = JobManager(settings(tmp_path), browser)
        parent = Job.create(
            "consult", "first", model="parent-model", quality="pro",
            reasoning_effort="long", allow_downgrade=True,
        )
        parent.state = "COMPLETE"
        parent.conversation_url = conversation_url
        manager.store.save(parent)
        expected_selection = {
            "model": "replacement-model" if override else "parent-model",
            "quality": "extra_high" if override else "pro",
            "reasoning_effort": "extended" if override else "long",
            "allow_downgrade": not override,
        }
        upload_ids = []
        if with_upload:
            upload = await manager.uploads.put("follow-up.txt", "text/plain", b"follow-up source")
            upload_ids.append(upload.upload_id)
        try:
            job = await manager.continue_task(
                parent.id,
                "continue",
                upload_ids=upload_ids,
                **(expected_selection if override else {}),
            )
            current = await wait_terminal(manager, job.id)
            assert current.state == "COMPLETE"
            assert browser.continued == [{
                "conversation_url": conversation_url,
                **expected_selection,
                "attachment_bytes": [b"follow-up source"] if with_upload else [],
            }]
            assert browser.submitted == [current.sending_text]
            assert current.prompt == "continue"
            assert current.sending_text.startswith("【CODEX_ORACLE】\n")
            assert current.sending_text.endswith("\n\ncontinue")
            assert current.conversation_url == conversation_url
            assert current.parent_id == parent.id
            assert current.assistant_count_before == 3
            assert current.resolved_model == expected_selection["model"]
            assert current.resolved_reasoning_effort == expected_selection["reasoning_effort"]
            assert current.selection_source == "test"
            assert current.selection_verified_at == "2026-10-02T00:00:00+00:00"
            assert bool(current.attachments) is with_upload
            if with_upload:
                assert current.attachments[0]["verified"] is True
                assert current.attachments[0]["sha256"] == upload.sha256
            assert manager.get(parent.id).model == "parent-model"
        finally:
            await manager.stop()

    asyncio.run(run())


@pytest.mark.parametrize("mode,expected_quality", [
    ("consult", "extra_high"), ("critic", "extra_high"), ("research", "pro"),
])
def test_continuation_auto_quality_uses_task_mode(
    tmp_path: Path, mode: str, expected_quality: str,
) -> None:
    async def run() -> None:
        conversation_url = "https://chatgpt.com/c/test"
        browser = FakeBrowser([
            BrowserSnapshot("COMPLETE", conversation_url, result_text="continued answer"),
            BrowserSnapshot("COMPLETE", conversation_url, result_text="continued answer"),
        ])
        manager = JobManager(settings(tmp_path), browser)
        parent = Job.create(mode, "first")
        parent.state = "COMPLETE"
        parent.conversation_url = conversation_url
        manager.store.save(parent)
        try:
            job = await manager.continue_task(parent.id, "continue")
            current = await wait_terminal(manager, job.id)
            assert current.state == "COMPLETE"
            assert current.quality == "auto"
            assert browser.continued[0]["quality"] == expected_quality
            assert current.resolved_reasoning_effort == expected_quality
        finally:
            await manager.stop()

    asyncio.run(run())


@pytest.mark.parametrize("with_upload", [False, True])
def test_continuation_model_unavailable_does_not_send(tmp_path: Path, with_upload: bool) -> None:
    async def run() -> None:
        browser = FakeBrowser([])
        browser.raise_model_unavailable = True
        manager = JobManager(settings(tmp_path), browser)
        parent = Job.create("consult", "first", quality="pro")
        parent.state = "COMPLETE"
        parent.conversation_url = "https://chatgpt.com/c/test"
        manager.store.save(parent)
        upload_ids = []
        if with_upload:
            upload = await manager.uploads.put("follow-up.txt", "text/plain", b"source")
            upload_ids.append(upload.upload_id)
        try:
            job = await manager.continue_task(parent.id, "continue", upload_ids=upload_ids)
            current = await wait_terminal(manager, job.id)
            assert current.state == "MODEL_UNAVAILABLE"
            assert current.error_code == "MODEL_UNAVAILABLE"
            assert current.resolved_model is None
            assert current.resolved_reasoning_effort is None
            assert current.selection_verified_at is None
            assert manager.result(job.id)[1] is None
            assert browser.submitted == []
            assert browser.continued == []
        finally:
            await manager.stop()

    asyncio.run(run())
