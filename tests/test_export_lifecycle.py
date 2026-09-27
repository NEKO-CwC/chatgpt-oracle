from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from chatgpt_oracle.browser import BrowserNeedsUserAction
from chatgpt_oracle.config import Settings
from chatgpt_oracle.exports import ExportSessionMismatch
from chatgpt_oracle.jobs import ExportRequestError, JobManager
from chatgpt_oracle.spooling import PagedMessageSpool


class ExportFixtureBrowser:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.closed: list[str] = []
        self.download_calls: list[tuple[str, str | None, str | None]] = []

    async def export_conversation(self, url: str, **_: object) -> dict[str, object]:
        self.reads.append(url)
        return {
            "source": "fixture",
            "completeness": {
                "messages": "verified",
                "attachments": "verified",
                "overall": "verified",
            },
            "messages": [
                {"id": "u1", "role": "user", "text": "question"},
                {"id": "a1", "role": "assistant", "text": "answer🙂"},
            ],
            "attachments": [
                {
                    "attachment_id": "att-1",
                    "message_id": "a1",
                    "filename": "result.zip",
                    "mime_type": "application/zip",
                    "source_url": "https://chatgpt.com/files/result.zip",
                }
            ],
        }

    async def has_conversation_tab(self, url: str) -> bool:
        return False

    async def start(self) -> None:
        return None

    async def close_conversation_tabs(self, url: str) -> bool:
        self.closed.append(url)
        return True

    async def download_attachment(
        self,
        url: str,
        *,
        attachment_id: str | None = None,
        conversation_url: str | None = None,
    ) -> tuple[bytes, str, str | None]:
        self.download_calls.append((url, attachment_id, conversation_url))
        raise BrowserNeedsUserAction("download_failed", "fixture failure")

    async def close(self) -> None:
        return None


class LoginRequiredBrowser(ExportFixtureBrowser):
    async def export_conversation(self, url: str, **_: object) -> dict[str, object]:
        raise BrowserNeedsUserAction("login_required", "请在专用浏览器中完成登录。")


class SlowExportBrowser(ExportFixtureBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def export_conversation(self, url: str, **kwargs: object) -> dict[str, object]:
        self.started.set()
        await self.release.wait()
        return await super().export_conversation(url, **kwargs)

    async def close_conversation_tabs(self, url: str) -> bool:
        self.closed.append(url)
        self.release.set()
        return True


def _manager(tmp_path: Path) -> tuple[JobManager, ExportFixtureBrowser]:
    settings = replace(
        Settings.from_env(tmp_path),
        jobs_dir=tmp_path / "jobs",
        sessions_dir=tmp_path / "sessions",
        browser_profile_dir=tmp_path / "profile",
        export_ttl_seconds=60,
        export_max_snapshot_bytes=1024 * 1024,
    )
    browser = ExportFixtureBrowser()
    return JobManager(settings, browser), browser


async def _wait_ready(manager: JobManager, session_id: str, export_id: str) -> dict[str, object]:
    for _ in range(200):
        status = await manager.export_status(session_id, export_id)
        if status["state"] != "QUEUED" and status["state"] != "RUNNING":
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("export did not reach a terminal or READY state")


def test_export_start_pages_all_views_and_releases(tmp_path: Path) -> None:
    async def run() -> None:
        manager, browser = _manager(tmp_path)
        started = await manager.export_start(
            conversation_url="https://chatgpt.com/c/test-export",
            request_id="archive-one",
        )
        assert started["state"] == "QUEUED"
        ready = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"
        assert ready["source"] == "fixture"
        assert ready["completeness"]["overall"] == "verified"
        assert ready["message_count"] == 2
        assert ready["attachment_count"] == 1

        pages = await manager.export_page(
            session_id=started["session_id"],
            export_id=started["export_id"],
            view="messages",
            cursor=None,
        )
        assert pages["eof"] is True
        assert [message["id"] for message in pages["messages"]] == ["u1", "a1"]
        assert pages["messages"][1]["text"] == "answer🙂"
        assert pages["message_segment"] is None
        assert "source_url" not in repr(pages)

        handle = await manager.export_attachment_handle(
            session_id=started["session_id"],
            export_id=started["export_id"],
            attachment_id="att-1",
        )
        assert handle["download_url"].startswith("/exports/download/dl_")
        released = await manager.export_release(started["session_id"], started["export_id"])
        assert released["state"] == "RELEASED"
        assert browser.reads == ["https://chatgpt.com/c/test-export"]
        await manager.stop()

    asyncio.run(run())


def test_job_manager_consumes_and_closes_streamed_message_spool(tmp_path: Path) -> None:
    async def run() -> None:
        spool = PagedMessageSpool(tmp_path / "ingest", max_bytes=1024 * 1024)
        raw_page = {
            "messages": [{
                "id": "m1",
                "author": {"role": "assistant"},
                "content": {"parts": ["streamed result"]},
            }],
            "page_info": {"has_previous_page": False, "has_next_page": False},
        }
        normalized_page = {
            "messages": [{"id": "m1", "role": "assistant", "text": "streamed result", "attachments": []}],
            "attachments": [],
            "excluded_system_message_count": 0,
        }
        spool.append_page(raw_page, normalized_page, cursor_matches=True)
        spool.finalize()

        class SpoolBrowser(ExportFixtureBrowser):
            def __init__(self) -> None:
                super().__init__()
                self.stream_pages_received = False

            async def export_conversation(self, url: str, **kwargs: object) -> dict[str, object]:
                self.stream_pages_received = kwargs.get("stream_pages") is True
                return {
                    "source": "api_paged",
                    "completeness": {
                        "messages": "verified",
                        "attachments": "verified",
                        "overall": "verified",
                    },
                    "messages": spool,
                    "message_spool": spool,
                    "attachments": [],
                }

        manager, _ = _manager(tmp_path)
        browser = SpoolBrowser()
        manager.browser = browser
        started = await manager.export_start(
            conversation_url="https://chatgpt.com/c/spool-manager",
            scope="all",
        )
        ready = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"
        page = await manager.export_page(
            session_id=started["session_id"],
            export_id=started["export_id"],
            view="messages",
            cursor=None,
        )
        assert browser.stream_pages_received is True
        assert [message["text"] for message in page["messages"]] == ["streamed result"]
        assert not spool._root.exists()
        await manager.stop()

    asyncio.run(run())


def test_release_does_not_cancel_a_running_export(tmp_path: Path) -> None:
    async def run() -> None:
        manager, browser = _manager(tmp_path)
        browser = SlowExportBrowser()
        manager.browser = browser
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/release-running")
        await asyncio.wait_for(browser.started.wait(), timeout=2)

        with pytest.raises(ExportRequestError) as error:
            await manager.export_release(started["session_id"], started["export_id"])

        assert error.value.code == "EXPORT_NOT_READY"
        assert manager.scheduler.get_export(started["export_id"])["state"] == "RUNNING"
        cancelled = await manager.export_cancel(started["session_id"], started["export_id"])
        assert cancelled["state"] == "CANCELLED"
        assert manager.scheduler.status()["active_task_id"] is None
        await manager.stop()

    asyncio.run(run())


def test_cancel_releases_the_browser_slot_and_is_audited(tmp_path: Path) -> None:
    async def run() -> None:
        manager, _ = _manager(tmp_path)
        browser = SlowExportBrowser()
        manager.browser = browser
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/cancel-running")
        await asyncio.wait_for(browser.started.wait(), timeout=2)

        cancelled = await manager.export_cancel(started["session_id"], started["export_id"])

        assert cancelled["state"] == "CANCELLED"
        assert manager.scheduler.status()["active_task_id"] is None
        events = manager.scheduler.export_events(started["export_id"])
        assert events[-1]["event"] == "CANCELLED"
        assert events[-1]["metadata"] == {"operation": "cancel"}
        await manager.stop()

    asyncio.run(run())


def test_clean_manager_stop_requeues_active_export(tmp_path: Path) -> None:
    async def run() -> None:
        first_settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
        )
        slow_browser = SlowExportBrowser()
        first_manager = JobManager(first_settings, slow_browser)
        started = await first_manager.export_start(conversation_url="https://chatgpt.com/c/stop-requeue")
        await asyncio.wait_for(slow_browser.started.wait(), timeout=2)

        await first_manager.stop()

        record = first_manager.scheduler.get_export(started["export_id"])
        assert record["state"] == "QUEUED"
        assert first_manager.scheduler.status(started["export_id"])["scheduler_state"] == "QUEUED"
        assert first_manager.scheduler.status()["active_task_id"] is None
        assert first_manager.scheduler.export_events(started["export_id"])[-1]["event"] == "REQUEUED_AFTER_STOP"

        second_manager, _ = _manager(tmp_path)
        await second_manager.start()
        for _ in range(200):
            status = await second_manager.export_status(started["session_id"], started["export_id"])
            if status["state"] not in {"QUEUED", "RUNNING"}:
                break
            await asyncio.sleep(0.01)
        assert status["state"] == "READY", {
            "status": status,
            "scheduler": second_manager.scheduler.status(started["export_id"]),
            "events": second_manager.scheduler.export_events(started["export_id"]),
        }
        assert status["state"] == "READY"
        await second_manager.stop()

    asyncio.run(run())


def test_export_pages_continue_past_previous_4096_request_limit(tmp_path: Path) -> None:
    async def run() -> None:
        manager, _ = _manager(tmp_path)
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/page-limit")
        ready = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"
        manager.scheduler.update_export(started["export_id"], page_requests=4096)

        page = await manager.export_page(
            session_id=started["session_id"],
            export_id=started["export_id"],
            view="messages",
            cursor=None,
        )
        assert page["eof"] is True
        record = manager.scheduler.get_export(started["export_id"])
        assert record["page_requests"] == 4097
        event = manager.scheduler.export_events(started["export_id"])[-1]
        assert event["event"] == "VIEW_EOF"
        assert event["metadata"]["page_request_count"] == 4097
        await manager.stop()

    asyncio.run(run())


def test_snapshot_ttl_expires_without_status_access(tmp_path: Path) -> None:
    async def run() -> None:
        manager, _ = _manager(tmp_path)
        manager.settings = replace(manager.settings, export_ttl_seconds=0.5)
        manager.exports.ttl_seconds = 0.5
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/idle-expiry")
        ready = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"

        await asyncio.sleep(0.8)

        record = manager.scheduler.get_export(started["export_id"])
        assert record["state"] == "GONE"
        event = manager.scheduler.export_events(started["export_id"])[-1]
        assert event["event"] == "GONE"
        assert event["metadata"] == {"reason": "snapshot_ttl"}
        await manager.stop()

    asyncio.run(run())


def test_export_request_id_is_session_scoped_and_input_bound(tmp_path: Path) -> None:
    async def run() -> None:
        manager, _ = _manager(tmp_path)
        url = "https://chatgpt.com/c/idempotency"
        first = await manager.export_start(conversation_url=url, scope="all", request_id="same")
        retry = await manager.export_start(conversation_url=url, scope="all", request_id="same")
        assert retry["export_id"] == first["export_id"]
        assert retry["idempotent"] is True

        with pytest.raises(ExportRequestError) as conflict:
            await manager.export_start(
                conversation_url=url,
                scope="recent",
                request_id="same",
                on_busy="queue",
            )
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

        other = await manager.export_start(
            conversation_url="https://chatgpt.com/c/other",
            scope="all",
            request_id="same",
            on_busy="queue",
        )
        assert other["session_id"] != first["session_id"]
        await manager.stop()

    asyncio.run(run())


def test_export_pages_and_release_are_bound_to_the_session(tmp_path: Path) -> None:
    async def run() -> None:
        manager, _ = _manager(tmp_path)
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/binding")
        ready = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"

        with pytest.raises(ExportSessionMismatch):
            await manager.export_page(
                session_id="another-session",
                export_id=started["export_id"],
                view="messages",
                cursor=None,
            )
        with pytest.raises(ExportSessionMismatch):
            await manager.export_release("another-session", started["export_id"])
        await manager.stop()

    asyncio.run(run())


def test_ready_snapshot_is_gone_after_manager_restart(tmp_path: Path) -> None:
    async def run() -> None:
        first_manager, _ = _manager(tmp_path)
        started = await first_manager.export_start(conversation_url="https://chatgpt.com/c/restart")
        ready = await _wait_ready(first_manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"
        await first_manager.stop()

        second_manager, _ = _manager(tmp_path)
        status = await second_manager.export_status(started["session_id"], started["export_id"])
        assert status["state"] == "GONE"
        assert status["error_code"] == "EXPORT_GONE"
        assert second_manager.scheduler.export_events(started["export_id"])[-1]["metadata"] == {
            "reason": "snapshot_not_retained"
        }
        await second_manager.stop()

    asyncio.run(run())


def test_session_close_revokes_ready_snapshots_and_handles(tmp_path: Path) -> None:
    async def run() -> None:
        manager, browser = _manager(tmp_path)
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/close-session")
        ready = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"
        handle = await manager.export_attachment_handle(
            session_id=started["session_id"],
            export_id=started["export_id"],
            attachment_id="att-1",
        )
        closed = await manager.session_close(started["session_id"])
        assert closed["state"] == "DEAD"
        assert manager.scheduler.get_export(started["export_id"])["state"] == "RELEASED"
        assert manager.exports.handle(handle["download_url"].rsplit("/", 1)[-1]) is None
        assert browser.closed == ["https://chatgpt.com/c/close-session"]
        await manager.stop()

    asyncio.run(run())


def test_failed_download_can_get_a_fresh_handle(tmp_path: Path) -> None:
    async def run() -> None:
        manager, browser = _manager(tmp_path)
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/retry-file")
        ready = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert ready["state"] == "READY"
        first = await manager.export_attachment_handle(
            session_id=started["session_id"],
            export_id=started["export_id"],
            attachment_id="att-1",
        )
        with pytest.raises(BrowserNeedsUserAction):
            await manager.download_export_attachment(first["download_url"])
        assert browser.download_calls == [(
            "https://chatgpt.com/files/result.zip",
            "att-1",
            "https://chatgpt.com/c/retry-file",
        )]
        second = await manager.export_attachment_handle(
            session_id=started["session_id"],
            export_id=started["export_id"],
            attachment_id="att-1",
        )
        assert second["download_url"] != first["download_url"]
        await manager.stop()

    asyncio.run(run())


def test_export_preserves_manual_user_action_as_a_terminal_state(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
        )
        manager = JobManager(settings, LoginRequiredBrowser())
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/login-required")
        status = await _wait_ready(manager, started["session_id"], started["export_id"])
        assert status["state"] == "NEEDS_USER_INPUT"
        assert status["instructions"] == "请在专用浏览器中完成登录。"
        assert manager.scheduler.get_export(started["export_id"])["error_code"] == "NEEDS_USER_INPUT"
        await manager.stop()

    asyncio.run(run())


def test_session_close_cancels_only_its_running_export(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
        )
        browser = SlowExportBrowser()
        manager = JobManager(settings, browser)
        started = await manager.export_start(conversation_url="https://chatgpt.com/c/cancel-running")
        await asyncio.wait_for(browser.started.wait(), timeout=2)
        closed = await manager.session_close(started["session_id"])
        assert closed["state"] == "DEAD"

        for _ in range(100):
            status = await manager.export_status(started["session_id"], started["export_id"])
            if status["state"] == "CANCELLED":
                break
            await asyncio.sleep(0.01)
        assert status["state"] == "CANCELLED"
        assert browser.closed == ["https://chatgpt.com/c/cancel-running"]
        await manager.stop()

    asyncio.run(run())
