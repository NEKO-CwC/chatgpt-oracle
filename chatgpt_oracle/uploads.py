from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Settings


@dataclass(frozen=True)
class UploadMetadata:
    upload_id: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    created_at: float
    expires_at: float

    def public(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class InMemoryUpload:
    metadata: UploadMetadata
    data: bytes


@dataclass
class _UploadEntry:
    upload: InMemoryUpload
    attached_job_id: str | None = None


class UploadError(ValueError):
    """A client-correctable upload request error."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class UploadNotFound(UploadError):
    pass


class UploadStore:
    """Short-lived, process-local upload storage with no filesystem persistence."""

    _filename_re = re.compile(r"[\x00-\x1f\x7f]")

    def __init__(self, settings: Settings):
        self.max_file_bytes = settings.upload_max_file_bytes
        self.max_total_bytes = settings.upload_max_total_bytes
        self.max_files = settings.upload_max_files
        self.ttl_seconds = settings.upload_ttl_seconds
        self._entries: dict[str, _UploadEntry] = {}
        self._lock = asyncio.Lock()
        self._reserved_bytes = 0
        self._active_streams = 0

    @property
    def reserved_bytes(self) -> int:
        return self._reserved_bytes

    async def put(
        self,
        filename: str,
        mime_type: str | None,
        data: bytes,
    ) -> UploadMetadata:
        self._validate_filename(filename)
        if len(data) > self.max_file_bytes:
            raise UploadError(f"file exceeds the {self.max_file_bytes} byte limit", 413)
        async with self._lock:
            self._purge_expired_locked()
            if len(self._entries) + self._active_streams >= self.max_files:
                raise UploadError(f"too many staged uploads; limit is {self.max_files}", 429)
            if self._reserved_bytes + len(data) > self.max_total_bytes:
                raise UploadError("upload memory limit reached; retry after existing uploads are consumed", 429)
            return self._insert_locked(filename, mime_type, data)

    async def put_stream(
        self,
        filename: str,
        mime_type: str | None,
        receive: Callable[[], Awaitable[dict[str, Any]]],
        *,
        declared_length: int | None = None,
    ) -> UploadMetadata:
        self._validate_filename(filename)
        if declared_length is not None and declared_length < 0:
            raise UploadError("Content-Length must not be negative")
        if declared_length is not None and declared_length > self.max_file_bytes:
            raise UploadError(f"file exceeds the {self.max_file_bytes} byte limit", 413)
        async with self._lock:
            self._purge_expired_locked()
            if len(self._entries) + self._active_streams >= self.max_files:
                raise UploadError(f"too many staged uploads; limit is {self.max_files}", 429)
            self._active_streams += 1
        chunks: list[bytes] = []
        total = 0
        inserted = False
        released = False
        deadline = time.monotonic() + self.ttl_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UploadError("upload stream timed out", 408)
                try:
                    message = await asyncio.wait_for(receive(), timeout=min(60.0, remaining))
                except asyncio.TimeoutError as exc:
                    raise UploadError("upload stream timed out", 408) from exc
                if message.get("type") == "http.disconnect":
                    raise UploadError("client disconnected during upload")
                if message.get("type") != "http.request":
                    raise UploadError("unexpected upload stream event")
                chunk = message.get("body", b"")
                if chunk:
                    if total + len(chunk) > self.max_file_bytes:
                        raise UploadError(f"file exceeds the {self.max_file_bytes} byte limit", 413)
                    async with self._lock:
                        if self._reserved_bytes + len(chunk) > self.max_total_bytes:
                            raise UploadError("upload memory limit reached", 429)
                        self._reserved_bytes += len(chunk)
                        total += len(chunk)
                    chunks.append(bytes(chunk))
                if not message.get("more_body", False):
                    break
            if declared_length is not None and total != declared_length:
                raise UploadError("request body size does not match Content-Length")
            data = b"".join(chunks)
            async with self._lock:
                self._reserved_bytes -= total
                released = True
                metadata = self._insert_locked(filename, mime_type, data)
                inserted = True
                return metadata
        finally:
            async with self._lock:
                self._active_streams -= 1
                if not inserted and not released:
                    self._reserved_bytes -= total

    def _insert_locked(self, filename: str, mime_type: str | None, data: bytes) -> UploadMetadata:
        now = time.time()
        upload_id = f"upl_{uuid.uuid4().hex[:24]}"
        metadata = UploadMetadata(
            upload_id=upload_id,
            filename=self._normalise_filename(filename),
            mime_type=(mime_type or "application/octet-stream").split(";", 1)[0].strip(),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._entries[upload_id] = _UploadEntry(InMemoryUpload(metadata, bytes(data)))
        self._reserved_bytes += len(data)
        return metadata

    async def attach(self, upload_ids: list[str], job_id: str) -> list[InMemoryUpload]:
        if len(upload_ids) > self.max_files:
            raise UploadError(f"too many files; limit is {self.max_files}")
        if len(set(upload_ids)) != len(upload_ids):
            raise UploadError("upload_ids must not contain duplicates")
        async with self._lock:
            self._purge_expired_locked()
            entries: list[_UploadEntry] = []
            for upload_id in upload_ids:
                entry = self._entries.get(upload_id)
                if entry is None:
                    raise UploadNotFound(f"upload not found or expired: {upload_id}")
                if entry.attached_job_id and entry.attached_job_id != job_id:
                    raise UploadError(f"upload is already attached to another task: {upload_id}")
                entries.append(entry)
            for entry in entries:
                entry.attached_job_id = job_id
            return [entry.upload for entry in entries]

    async def take_for_job(self, job_id: str, upload_ids: list[str]) -> list[InMemoryUpload]:
        async with self._lock:
            self._purge_expired_locked()
            uploads: list[InMemoryUpload] = []
            for upload_id in upload_ids:
                entry = self._entries.get(upload_id)
                if entry is None or entry.attached_job_id != job_id:
                    raise UploadNotFound(f"upload not found for task: {upload_id}")
                uploads.append(entry.upload)
            return uploads

    async def release_job(self, job_id: str) -> None:
        async with self._lock:
            remove = [
                upload_id
                for upload_id, entry in self._entries.items()
                if entry.attached_job_id == job_id
            ]
            for upload_id in remove:
                entry = self._entries.pop(upload_id)
                self._reserved_bytes -= entry.upload.metadata.size

    async def release_unattached(self, upload_id: str) -> None:
        async with self._lock:
            entry = self._entries.get(upload_id)
            if entry and entry.attached_job_id is None:
                self._entries.pop(upload_id)
                self._reserved_bytes -= entry.upload.metadata.size

    async def metadata(self, upload_id: str) -> UploadMetadata:
        async with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(upload_id)
            if entry is None:
                raise UploadNotFound(f"upload not found or expired: {upload_id}")
            return entry.upload.metadata

    def _purge_expired_locked(self) -> None:
        now = time.time()
        remove = [
            upload_id
            for upload_id, entry in self._entries.items()
            if entry.upload.metadata.expires_at <= now and entry.attached_job_id is None
        ]
        for upload_id in remove:
            entry = self._entries.pop(upload_id)
            self._reserved_bytes -= entry.upload.metadata.size

    @classmethod
    def _validate_filename(cls, filename: str) -> None:
        if not filename or len(filename) > 255 or cls._filename_re.search(filename):
            raise UploadError("filename is empty, too long, or contains control characters")

    @staticmethod
    def _normalise_filename(filename: str) -> str:
        # Keep the client's visible basename while preventing path traversal in
        # browser UI and receipts. The file bytes never touch the filesystem.
        return Path(filename.replace("\\", "/")).name or "upload.bin"
