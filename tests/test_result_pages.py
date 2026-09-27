from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.file_store import FileTransferError
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job


class ResultBrowser:
    async def close(self) -> None:
        return None


def test_result_pages_reconstruct_utf8_without_gaps_or_overlaps(tmp_path: Path) -> None:
    result = ("病历字段🙂ABCD\n" * 1800).strip()
    expected = result.encode("utf-8")
    jobs_dir = tmp_path / "jobs"
    settings = Settings.from_env(tmp_path)
    manager = JobManager(
        replace(
            settings,
            jobs_dir=jobs_dir,
            sessions_dir=tmp_path / "sessions",
            browser_profile_dir=tmp_path / "profile",
        ),
        ResultBrowser(),
    )

    async def run() -> None:
        try:
            session = manager.sessions.create("result-agent")
            job = Job.create("consult", "read result", session_id=session.id)
            job.state = "COMPLETE"
            manager.store.write_result(job, result)
            manager.store.save(job)

            offset = 0
            cursor: str | None = None
            pages: list[str] = []
            while True:
                page = manager.result_page(session.id, job.id, cursor=cursor, max_response_bytes=512)
                assert page["byte_offset"] == offset
                assert page["total_bytes"] == len(expected)
                assert page["sha256"] == hashlib.sha256(expected).hexdigest()
                pages.append(page["text"])
                offset += page["returned_bytes"]
                if page["eof"]:
                    assert page["next_cursor"] is None
                    break
                cursor = page["next_cursor"]

            assert "".join(pages).encode("utf-8") == expected
            with pytest.raises(FileTransferError, match="does not belong"):
                manager.result_page("other-agent", job.id, cursor=None, max_response_bytes=512)
            reference = manager.result_reference(job.id)
            assert reference["ready"] is True
            assert reference["total_bytes"] == len(expected)
            assert "text" not in reference
        finally:
            await manager.stop()

    asyncio.run(run())
