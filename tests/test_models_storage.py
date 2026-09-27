from __future__ import annotations

from pathlib import Path

from chatgpt_oracle.models import Job, public_job
from chatgpt_oracle.storage import JobStore


def test_job_round_trip_and_prompt_hash(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = Job.create("research", "Find sources about MCP transport")
    store.save(job)
    loaded = store.get(job.id)
    assert loaded is not None
    assert loaded.prompt == job.prompt
    assert loaded.prompt_sha256 == job.prompt_sha256
    assert public_job(loaded)["result_available"] is False


def test_result_is_atomic_and_readable(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = Job.create("consult", "Answer a small question")
    store.save(job)
    store.write_result(job, "# Answer\n\nDone")
    store.save(job)
    assert store.read_result(store.get(job.id)) == "# Answer\n\nDone"

