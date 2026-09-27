from __future__ import annotations

import json
import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .models import Job


class JobStore:
    """Small durable JSON store; one file per task keeps recovery inspectable."""

    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self.jobs_dir / f"{task_id}.json"

    def _result_path(self, task_id: str) -> Path:
        return self.jobs_dir / f"{task_id}.md"

    def save(self, job: Job) -> None:
        path = self._path(job.id)
        fd, temp_name = tempfile.mkstemp(prefix=f"{job.id}.", suffix=".tmp", dir=self.jobs_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(job.as_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def get(self, task_id: str) -> Job | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return Job.from_dict(json.load(handle))

    def all(self) -> list[Job]:
        jobs: list[Job] = []
        for path in sorted(self.jobs_dir.glob("cgo_*.json")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    jobs.append(Job.from_dict(json.load(handle)))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return sorted(jobs, key=lambda job: job.created_at)

    def find_recoverable(self) -> list[Job]:
        return [job for job in self.all() if job.state in {"QUEUED", "STARTING", "RUNNING"}]

    def write_result(self, job: Job, content: str) -> Path:
        path = self._result_path(job.id)
        temp = path.with_suffix(".md.tmp")
        temp.write_bytes(content.encode("utf-8"))
        os.replace(temp, path)
        job.result_path = str(path)
        return path

    def read_result(self, job: Job) -> str | None:
        if not job.result_path:
            return None
        path = Path(job.result_path)
        if not path.is_absolute():
            path = self.jobs_dir / path
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def read_result_page(
        self,
        job: Job,
        *,
        cursor: str | None,
        max_response_bytes: int,
    ) -> dict[str, object]:
        if max_response_bytes < 512 or max_response_bytes > 12 * 1024:
            raise ValueError("max_response_bytes is outside the supported range")
        path = self._result_path(job.id)
        if not job.result_path or Path(job.result_path).resolve() != path.resolve() or not path.is_file():
            raise FileNotFoundError("completed result is unavailable")
        total_bytes = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as result_file:
            while block := result_file.read(256 * 1024):
                digest.update(block)
        sha256 = digest.hexdigest()
        offset = 0
        if cursor is not None:
            try:
                padding = "=" * (-len(cursor) % 4)
                payload = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
                descriptor = json.loads(payload.decode("utf-8"))
                if not isinstance(descriptor, dict):
                    raise ValueError("cursor must contain an object")
                if descriptor.get("result_id") != job.id or descriptor.get("sha256") != sha256:
                    raise ValueError("cursor belongs to another result revision")
                offset = descriptor["offset"]
                if type(offset) is not int or offset < 0 or offset > total_bytes:
                    raise ValueError("cursor offset is invalid")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("result cursor is invalid or belongs to another result") from exc
        if offset < total_bytes:
            with path.open("rb") as result_file:
                result_file.seek(offset)
                first = result_file.read(1)
                if first and first[0] & 0xC0 == 0x80:
                    raise ValueError("result cursor splits a UTF-8 code point")
        with path.open("rb") as result_file:
            result_file.seek(offset)
            content = result_file.read(max_response_bytes)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.start == 0:
                raise ValueError("stored result contains invalid UTF-8") from exc
            content = content[:exc.start]
            text = content.decode("utf-8")
        next_offset = offset + len(content)
        eof = next_offset >= total_bytes
        next_cursor = None
        if not eof:
            encoded = json.dumps(
                {"result_id": job.id, "sha256": sha256, "offset": next_offset},
                separators=(",", ":"),
            ).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
        return {
            "result_id": job.id,
            "byte_offset": offset,
            "returned_bytes": len(content),
            "total_bytes": total_bytes,
            "sha256": sha256,
            "text": text,
            "eof": eof,
            "next_cursor": next_cursor,
        }
