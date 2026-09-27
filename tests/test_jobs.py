from __future__ import annotations

import asyncio
from pathlib import Path

from chatgpt_oracle.browser import BrowserSnapshot, ModelUnavailableError, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.jobs import JobManager


class FakeBrowser:
    def __init__(self, snapshots: list[BrowserSnapshot]):
        self.snapshots = list(snapshots)
        self.submitted: list[str] = []
        self.qualities: list[str] = []
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
        )

    async def continue_conversation(self, conversation_url: str, prompt: str) -> SubmitReceipt:
        self.submitted.append(prompt)
        return SubmitReceipt(conversation_url, 0)

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
