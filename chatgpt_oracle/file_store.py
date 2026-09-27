from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TRANSFER_ID_RE = re.compile(r"tr_[0-9a-f]{32}\Z")
_FILE_REF_RE = re.compile(r"fref_[0-9a-f]{32}\Z")
_UNSAFE_FILENAME_RE = re.compile(r"[\x00-\x1f\x7f]")


class FileTransferError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class FileRecord:
    session_id: str
    file_ref: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    representation: str
    provenance: str
    expires_at: float
    path: Path = field(repr=False, compare=False)

    def public(self, *, content_url: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session_id": self.session_id,
            "file_ref": self.file_ref,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
            "representation": self.representation,
            "provenance": self.provenance,
            "expires_at": self.expires_at,
        }
        if content_url is not None:
            result["download_url"] = content_url
            result["download_headers"] = {"X-Oracle-Session-ID": self.session_id}
        return result


class FileStore:
    """SQLite-backed, session-scoped file metadata with bounded disk streaming."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int,
        max_total_bytes: int,
        max_transfers: int,
        max_chunk_bytes: int,
        io_chunk_bytes: int,
        ttl_seconds: float,
        metadata_retention_seconds: float,
    ):
        self.root = root.resolve()
        self.objects_dir = self.root / "objects"
        self.database = self.root / "files.sqlite3"
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_transfers = max_transfers
        self.max_chunk_bytes = max_chunk_bytes
        self.io_chunk_bytes = io_chunk_bytes
        self.ttl_seconds = ttl_seconds
        self.metadata_retention_seconds = metadata_retention_seconds
        self._initialized = False
        self._initialize_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self._initialize()
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.objects_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database, timeout=30)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS file_transfers (
                    transfer_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    request_id TEXT,
                    request_hash TEXT,
                    file_ref TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    expected_size INTEGER NOT NULL,
                    expected_sha256 TEXT,
                    actual_sha256 TEXT,
                    file_kind TEXT,
                    committed_offset INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS file_transfer_request
                    ON file_transfers(session_id, request_id) WHERE request_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS file_transfer_expiry
                    ON file_transfers(state, expires_at);
                CREATE TABLE IF NOT EXISTS files (
                    file_ref TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    transfer_id TEXT NOT NULL UNIQUE REFERENCES file_transfers(transfer_id),
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    representation TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'READY',
                    active_readers INTEGER NOT NULL DEFAULT 0,
                    active_tasks INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS file_expiry ON files(state, expires_at);
                CREATE TABLE IF NOT EXISTS file_task_refs (
                    task_id TEXT NOT NULL,
                    file_ref TEXT NOT NULL REFERENCES files(file_ref),
                    session_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(task_id, file_ref)
                );
                CREATE INDEX IF NOT EXISTS file_task_lookup ON file_task_refs(task_id, ordinal);
                """
                )
            finally:
                connection.close()
            connection = sqlite3.connect(self.database, timeout=30)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(file_transfers)")}
                if "file_kind" not in columns:
                    connection.execute("ALTER TABLE file_transfers ADD COLUMN file_kind TEXT")
            finally:
                connection.close()
            self._initialized = True

    @staticmethod
    def _filename(value: str) -> str:
        if not value or len(value) > 255 or _UNSAFE_FILENAME_RE.search(value):
            raise FileTransferError("INVALID_FILENAME", "filename is empty, too long, or contains control characters")
        result = Path(value.replace("\\", "/")).name
        if result in {"", ".", ".."}:
            raise FileTransferError("INVALID_FILENAME", "filename has no usable name")
        return result

    @staticmethod
    def _session_id(value: str) -> str:
        if not value or len(value) > 128 or _UNSAFE_FILENAME_RE.search(value):
            raise FileTransferError("INVALID_SESSION", "session_id is empty or invalid")
        return value

    @staticmethod
    def _validate_ref(value: str, pattern: re.Pattern[str], label: str) -> None:
        if not pattern.fullmatch(value):
            raise FileTransferError("NOT_FOUND", f"{label} not found", 404)

    def _path(self, file_ref: str, filename: str | None = None) -> Path:
        self._validate_ref(file_ref, _FILE_REF_RE, "file")
        if filename is None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT filename FROM file_transfers WHERE file_ref=? UNION SELECT filename FROM files WHERE file_ref=? LIMIT 1",
                    (file_ref, file_ref),
                ).fetchone()
            if row is None:
                raise FileTransferError("NOT_FOUND", "file path not found", 404)
            filename = row["filename"]
        return self.objects_dir / file_ref / self._filename(filename)

    @staticmethod
    def _remove_path(path: Path) -> None:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def create_upload(
        self,
        *,
        session_id: str,
        filename: str,
        size: int,
        mime_type: str | None,
        sha256: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = self._session_id(session_id)
        filename = self._filename(filename)
        if size < 0 or size > self.max_file_bytes:
            raise FileTransferError("FILE_TOO_LARGE", "file size is outside the supported range", 413)
        if sha256 is not None and not _DIGEST_RE.fullmatch(sha256.lower()):
            raise FileTransferError("INVALID_SHA256", "sha256 must be a 64-character hexadecimal digest")
        if request_id is not None:
            if not request_id or len(request_id) > 128:
                raise FileTransferError("INVALID_REQUEST_ID", "request_id is empty or too long")
            if sha256 is None:
                raise FileTransferError(
                    "IDEMPOTENCY_REQUIRES_DIGEST",
                    "sha256 is required when request_id is supplied",
                )
        mime_type = (mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").split(";", 1)[0].strip()
        if not mime_type or "\r" in mime_type or "\n" in mime_type:
            raise FileTransferError("INVALID_MIME_TYPE", "mime_type is invalid")
        now = time.time()
        request_hash = hashlib.sha256(
            f"{filename}\0{mime_type}\0{size}\0{(sha256 or '').lower()}".encode("utf-8")
        ).hexdigest()
        transfer_id = f"tr_{uuid.uuid4().hex}"
        file_ref = f"fref_{uuid.uuid4().hex}"
        expires_at = now + self.ttl_seconds
        path = self._path(file_ref, filename)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_rows(connection, now)
            if request_id is not None:
                existing = connection.execute(
                    "SELECT * FROM file_transfers WHERE session_id=? AND request_id=?",
                    (session_id, request_id),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise FileTransferError(
                            "IDEMPOTENCY_CONFLICT", "request_id was used with different file metadata", 409
                        )
                    return self._transfer_public(existing, idempotent=True)
            active_count = int(
                connection.execute(
                    "SELECT count(*) FROM file_transfers WHERE state IN ('RECEIVING','VERIFYING','FETCHING')"
                ).fetchone()[0]
            )
            stored_bytes = int(
                connection.execute(
                    "SELECT COALESCE(sum(size),0) FROM files WHERE state='READY' OR (state='RELEASED' AND (active_readers>0 OR active_tasks>0))"
                ).fetchone()[0]
            )
            reserved_bytes = int(
                connection.execute(
                    "SELECT COALESCE(sum(expected_size),0) FROM file_transfers WHERE state IN ('RECEIVING','VERIFYING','FETCHING')"
                ).fetchone()[0]
            )
            if active_count >= self.max_transfers:
                raise FileTransferError("TRANSFER_CAPACITY", "too many active transfers", 429)
            if stored_bytes + reserved_bytes + size > self.max_total_bytes:
                raise FileTransferError("FILE_STORE_CAPACITY", "file storage limit reached", 429)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=False)
            initial_state = "VERIFYING" if size == 0 else "RECEIVING"
            connection.execute(
                """INSERT INTO file_transfers
                   (transfer_id,session_id,request_id,request_hash,file_ref,filename,mime_type,
                    expected_size,expected_sha256,state,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    transfer_id,
                    session_id,
                    request_id,
                    request_hash if request_id else None,
                    file_ref,
                    filename,
                    mime_type,
                    size,
                    sha256.lower() if sha256 else None,
                    initial_state,
                    now,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM file_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            assert row is not None
            result = self._transfer_public(row, idempotent=False)
        if size == 0:
            return self.verify_upload(transfer_id, session_id)
        return result

    def _transfer_public(self, row: sqlite3.Row, *, idempotent: bool | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "session_id": row["session_id"],
            "transfer_id": row["transfer_id"],
            "file_ref": row["file_ref"],
            "filename": row["filename"],
            "mime_type": row["mime_type"],
            "state": row["state"],
            "committed_offset": row["committed_offset"],
            "total_bytes": None if row["state"] == "FETCHING" else row["expected_size"],
            "phase": "fetching" if row["state"] == "FETCHING" else row["state"].lower(),
            "sha256": row["actual_sha256"] or row["expected_sha256"],
            "error_code": row["error_code"],
            "expires_at": row["expires_at"],
        }
        if idempotent is not None:
            value["idempotent"] = idempotent
        return value

    def create_download(
        self,
        *,
        session_id: str,
        filename: str,
        mime_type: str | None,
        source_identity: str,
        file_kind: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = self._session_id(session_id)
        filename = self._filename(filename)
        if not source_identity or len(source_identity) > 512:
            raise FileTransferError("INVALID_SOURCE", "source identity is empty or too long")
        mime_type = (mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").split(";", 1)[0].strip()
        if not mime_type or "\r" in mime_type or "\n" in mime_type:
            raise FileTransferError("INVALID_MIME_TYPE", "mime_type is invalid")
        now = time.time()
        reservation = self.max_file_bytes
        request_hash = hashlib.sha256(
            f"download\0{source_identity}\0{filename}\0{mime_type}\0{file_kind}".encode("utf-8")
        ).hexdigest()
        transfer_id = f"tr_{uuid.uuid4().hex}"
        file_ref = f"fref_{uuid.uuid4().hex}"
        path = self._path(file_ref, filename)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_rows(connection, now)
            if request_id:
                existing = connection.execute(
                    "SELECT * FROM file_transfers WHERE session_id=? AND request_id=?",
                    (session_id, request_id),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise FileTransferError("IDEMPOTENCY_CONFLICT", "request_id was used with a different source", 409)
                    return self._transfer_public(existing, idempotent=True)
            active_count = int(
                connection.execute(
                    "SELECT count(*) FROM file_transfers WHERE state IN ('RECEIVING','VERIFYING','FETCHING')"
                ).fetchone()[0]
            )
            stored_bytes = int(
                connection.execute(
                    "SELECT COALESCE(sum(size),0) FROM files WHERE state='READY' OR (state='RELEASED' AND (active_readers>0 OR active_tasks>0))"
                ).fetchone()[0]
            )
            reserved_bytes = int(
                connection.execute(
                    "SELECT COALESCE(sum(expected_size),0) FROM file_transfers WHERE state IN ('RECEIVING','VERIFYING','FETCHING')"
                ).fetchone()[0]
            )
            if active_count >= self.max_transfers:
                raise FileTransferError("TRANSFER_CAPACITY", "too many active transfers", 429)
            if stored_bytes + reserved_bytes + reservation > self.max_total_bytes:
                raise FileTransferError("FILE_STORE_CAPACITY", "file storage limit reached", 429)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=False)
            connection.execute(
                """INSERT INTO file_transfers
                   (transfer_id,session_id,request_id,request_hash,file_ref,filename,mime_type,
                    expected_size,state,file_kind,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?,'FETCHING',?,?,?)""",
                (
                    transfer_id,
                    session_id,
                    request_id,
                    request_hash if request_id else None,
                    file_ref,
                    filename,
                    mime_type,
                    reservation,
                    file_kind,
                    now,
                    now + self.ttl_seconds,
                ),
            )
            row = connection.execute(
                "SELECT * FROM file_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            assert row is not None
            return self._transfer_public(row, idempotent=False)

    def update_download_progress(self, transfer_id: str, session_id: str, offset: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(connection, transfer_id, session_id)
            if row["state"] != "FETCHING" or offset < row["committed_offset"] or offset > self.max_file_bytes:
                raise FileTransferError("DOWNLOAD_PROGRESS_INVALID", "download progress is outside the active transfer", 409)
            connection.execute(
                "UPDATE file_transfers SET committed_offset=? WHERE transfer_id=?",
                (offset, transfer_id),
            )

    def download_path(self, transfer_id: str, session_id: str) -> Path:
        with self._connect() as connection:
            row = self._transfer_row(connection, transfer_id, session_id)
            if row["state"] != "FETCHING":
                raise FileTransferError("TRANSFER_NOT_FETCHING", "download is not active", 409)
            return self._path(row["file_ref"], row["filename"])

    def rollback_unstarted_download(self, transfer_id: str, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(connection, transfer_id, session_id)
            if row["state"] != "FETCHING" or int(row["committed_offset"]) != 0:
                raise FileTransferError("DOWNLOAD_ALREADY_STARTED", "download can no longer be rolled back", 409)
            self._remove_path(self._path(row["file_ref"], row["filename"]))
            connection.execute("DELETE FROM file_transfers WHERE transfer_id=?", (transfer_id,))

    def complete_download(self, transfer_id: str, session_id: str, mime_type: str | None) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._transfer_row(connection, transfer_id, session_id)
        if row["state"] == "READY":
            return self._transfer_public(row)
        if row["state"] != "FETCHING":
            raise FileTransferError("TRANSFER_NOT_FETCHING", "download is not active", 409)
        path = self._path(row["file_ref"], row["filename"])
        size = path.stat().st_size
        if size > self.max_file_bytes:
            self.fail_download(session_id, transfer_id, "FILE_TOO_LARGE")
            raise FileTransferError("FILE_TOO_LARGE", "download exceeds the configured size limit", 413)
        actual_mime_type = (mime_type or row["mime_type"] or "application/octet-stream").split(";", 1)[0].strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._transfer_row(connection, transfer_id, session_id)
            if current["state"] != "FETCHING":
                raise FileTransferError("TRANSFER_NOT_FETCHING", "download changed before verification", 409)
            connection.execute(
                "UPDATE file_transfers SET committed_offset=?,expected_size=?,mime_type=?,state='VERIFYING' WHERE transfer_id=?",
                (size, size, actual_mime_type, transfer_id),
            )
        result = self.verify_upload(transfer_id, session_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT file_kind FROM file_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if row:
                kind = row["file_kind"] or "unknown"
                representation = "derived" if kind in {"image_asset_pointer", "simple_image_asset_pointer"} else "unknown"
                connection.execute(
                    "UPDATE files SET mime_type=?,representation=?,provenance='chatgpt_attachment' WHERE file_ref=?",
                    (actual_mime_type, representation, result["file_ref"]),
                )
        return self.transfer_status(transfer_id, session_id)

    def fail_download(self, session_id: str, transfer_id: str, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(connection, transfer_id, session_id)
            if row["state"] in {"FETCHING", "VERIFYING"}:
                self._remove_path(self._path(row["file_ref"], row["filename"]))
                connection.execute(
                    "UPDATE file_transfers SET state='FAILED',error_code=? WHERE transfer_id=?",
                    (error_code[:64], transfer_id),
                )

    def _transfer_row(self, connection: sqlite3.Connection, transfer_id: str, session_id: str) -> sqlite3.Row:
        self._validate_ref(transfer_id, _TRANSFER_ID_RE, "transfer")
        row = connection.execute(
            "SELECT * FROM file_transfers WHERE transfer_id=? AND session_id=?",
            (transfer_id, self._session_id(session_id)),
        ).fetchone()
        if row is None:
            raise FileTransferError("NOT_FOUND", "transfer not found", 404)
        if row["expires_at"] <= time.time() and row["state"] not in {"EXPIRED", "CANCELLED"}:
            raise FileTransferError("TRANSFER_EXPIRED", "transfer expired", 410)
        return row

    def transfer_status(self, transfer_id: str, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._transfer_public(self._transfer_row(connection, transfer_id, session_id))

    def append_chunk(self, transfer_id: str, session_id: str, offset: int, data: bytes) -> dict[str, Any]:
        if not data:
            raise FileTransferError("EMPTY_CHUNK", "an upload chunk must contain bytes")
        if len(data) > self.max_chunk_bytes:
            raise FileTransferError("CHUNK_TOO_LARGE", "chunk exceeds the supported size", 413)
        file_path: Path | None = None
        completed = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(connection, transfer_id, session_id)
            if row["state"] != "RECEIVING":
                raise FileTransferError("TRANSFER_NOT_RECEIVING", "transfer is not accepting bytes", 409)
            committed = int(row["committed_offset"])
            if offset != committed:
                raise FileTransferError("OFFSET_MISMATCH", f"expected Upload-Offset {committed}", 409)
            if committed + len(data) > int(row["expected_size"]):
                raise FileTransferError("UPLOAD_TOO_LONG", "chunk exceeds the declared Upload-Length", 413)
            file_path = self._path(row["file_ref"])
            size_on_disk = file_path.stat().st_size
            if size_on_disk < committed:
                connection.execute(
                    "UPDATE file_transfers SET state='FAILED',error_code='TRANSFER_CORRUPT' WHERE transfer_id=?",
                    (transfer_id,),
                )
                connection.commit()
                raise FileTransferError("TRANSFER_CORRUPT", "stored bytes are shorter than the committed offset", 500)
            with file_path.open("r+b") as file:
                if size_on_disk != committed:
                    file.truncate(committed)
                file.seek(committed)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            new_offset = committed + len(data)
            completed = new_offset == int(row["expected_size"])
            connection.execute(
                "UPDATE file_transfers SET committed_offset=?,state=? WHERE transfer_id=?",
                (new_offset, "VERIFYING" if completed else "RECEIVING", transfer_id),
            )
            result = self._transfer_row(connection, transfer_id, session_id)
            value = self._transfer_public(result)
        if completed:
            self.verify_upload(transfer_id, session_id)
            return self.transfer_status(transfer_id, session_id)
        return value

    def verify_upload(self, transfer_id: str, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._transfer_row(connection, transfer_id, session_id)
        if row["state"] == "READY":
            return self._transfer_public(row)
        if row["state"] != "VERIFYING":
            raise FileTransferError("TRANSFER_NOT_VERIFYING", "transfer is not ready for verification", 409)
        path = self._path(row["file_ref"])
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as file:
                while block := file.read(self.io_chunk_bytes):
                    digest.update(block)
                    total += len(block)
        except OSError as exc:
            raise FileTransferError("FILE_READ_ERROR", "uploaded file could not be verified", 500) from exc
        actual_sha256 = digest.hexdigest()
        expected_sha256 = row["expected_sha256"]
        if total != row["expected_size"] or (expected_sha256 and expected_sha256 != actual_sha256):
            with self._connect() as connection:
                connection.execute(
                    "UPDATE file_transfers SET state='FAILED',actual_sha256=?,error_code='CHECKSUM_MISMATCH' WHERE transfer_id=? AND state='VERIFYING'",
                    (actual_sha256, transfer_id),
                )
            self._remove_path(path)
            raise FileTransferError("CHECKSUM_MISMATCH", "uploaded file size or SHA-256 did not match", 422)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._transfer_row(connection, transfer_id, session_id)
            if current["state"] == "READY":
                return self._transfer_public(current)
            if current["state"] != "VERIFYING":
                raise FileTransferError("TRANSFER_NOT_VERIFYING", "transfer changed during verification", 409)
            connection.execute(
                """INSERT OR IGNORE INTO files
                   (file_ref,session_id,transfer_id,filename,mime_type,size,sha256,
                    representation,provenance,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,'unknown','caller_declared',?,?)""",
                (
                    current["file_ref"],
                    session_id,
                    transfer_id,
                    current["filename"],
                    current["mime_type"],
                    total,
                    actual_sha256,
                    now + self.ttl_seconds,
                    now,
                ),
            )
            connection.execute(
                "UPDATE file_transfers SET state='READY',actual_sha256=?,error_code=NULL,expires_at=? WHERE transfer_id=?",
                (actual_sha256, now + self.ttl_seconds, transfer_id),
            )
            ready = connection.execute(
                "SELECT * FROM file_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            assert ready is not None
            return self._transfer_public(ready)

    def get_file(self, session_id: str, file_ref: str) -> FileRecord:
        self._validate_ref(file_ref, _FILE_REF_RE, "file")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM files WHERE file_ref=? AND session_id=?",
                (file_ref, self._session_id(session_id)),
            ).fetchone()
            if row is None:
                raise FileTransferError("NOT_FOUND", "file not found", 404)
            if row["state"] != "READY" or row["expires_at"] <= time.time():
                raise FileTransferError("FILE_GONE", "file is no longer available", 410)
            return self._file_record(row)

    def attach_files_to_task(self, session_id: str, task_id: str, file_refs: list[str]) -> list[FileRecord]:
        session_id = self._session_id(session_id)
        if not task_id or len(task_id) > 128:
            raise FileTransferError("INVALID_TASK_ID", "task_id is empty or too long")
        if len(file_refs) != len(set(file_refs)):
            raise FileTransferError("DUPLICATE_FILE_REF", "files contains a duplicate file_ref")
        now = time.time()
        records: list[FileRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for ordinal, file_ref in enumerate(file_refs):
                self._validate_ref(file_ref, _FILE_REF_RE, "file")
                row = connection.execute(
                    "SELECT * FROM files WHERE file_ref=? AND session_id=? AND state='READY' AND expires_at>?",
                    (file_ref, session_id, now),
                ).fetchone()
                if row is None:
                    raise FileTransferError("FILE_GONE", "file is unavailable for this session", 410)
                connection.execute(
                    "INSERT INTO file_task_refs(task_id,file_ref,session_id,ordinal) VALUES(?,?,?,?)",
                    (task_id, file_ref, session_id, ordinal),
                )
                connection.execute(
                    "UPDATE files SET active_tasks=active_tasks+1 WHERE file_ref=?", (file_ref,)
                )
                records.append(self._file_record(row))
        return records

    def files_for_task(self, task_id: str) -> list[FileRecord]:
        if not task_id or len(task_id) > 128:
            raise FileTransferError("INVALID_TASK_ID", "task_id is empty or too long")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT f.* FROM files f JOIN file_task_refs r ON r.file_ref=f.file_ref
                   WHERE r.task_id=? AND f.state IN ('READY','RELEASED') ORDER BY r.ordinal""",
                (task_id,),
            ).fetchall()
        return [self._file_record(row) for row in rows]

    def release_task_files(self, task_id: str) -> None:
        if not task_id or len(task_id) > 128:
            raise FileTransferError("INVALID_TASK_ID", "task_id is empty or too long")
        remove: list[Path] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT f.* FROM files f JOIN file_task_refs r ON r.file_ref=f.file_ref
                   WHERE r.task_id=? ORDER BY r.ordinal""",
                (task_id,),
            ).fetchall()
            connection.execute("DELETE FROM file_task_refs WHERE task_id=?", (task_id,))
            for row in rows:
                active_tasks = max(0, int(row["active_tasks"]) - 1)
                expired = float(row["expires_at"]) <= time.time()
                if active_tasks == 0 and int(row["active_readers"]) == 0 and (
                    row["state"] == "RELEASED" or expired
                ):
                    path = self._path(row["file_ref"])
                    self._remove_path(path)
                    connection.execute("DELETE FROM files WHERE file_ref=?", (row["file_ref"],))
                    remove.append(path)
                else:
                    connection.execute(
                        "UPDATE files SET active_tasks=? WHERE file_ref=?",
                        (active_tasks, row["file_ref"]),
                    )

    def _file_record(self, row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            session_id=row["session_id"],
            file_ref=row["file_ref"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size=int(row["size"]),
            sha256=row["sha256"],
            representation=row["representation"],
            provenance=row["provenance"],
            expires_at=float(row["expires_at"]),
            path=self._path(row["file_ref"], row["filename"]),
        )

    def read_path(self, session_id: str, file_ref: str) -> tuple[FileRecord, Path]:
        self._validate_ref(file_ref, _FILE_REF_RE, "file")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM files WHERE file_ref=? AND session_id=?",
                (file_ref, self._session_id(session_id)),
            ).fetchone()
            if row is None:
                raise FileTransferError("NOT_FOUND", "file not found", 404)
            if row["state"] != "READY" or row["expires_at"] <= time.time():
                raise FileTransferError("FILE_GONE", "file is no longer available", 410)
            connection.execute("UPDATE files SET active_readers=active_readers+1 WHERE file_ref=?", (file_ref,))
            return self._file_record(row), self._path(file_ref, row["filename"])

    def finish_read(self, file_ref: str) -> None:
        self._validate_ref(file_ref, _FILE_REF_RE, "file")
        path_to_remove: Path | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM files WHERE file_ref=?", (file_ref,)).fetchone()
            if row is None:
                return
            readers = max(0, int(row["active_readers"]) - 1)
            if row["state"] == "RELEASED" and readers == 0 and int(row["active_tasks"]) == 0:
                path_to_remove = self._path(file_ref, row["filename"])
                self._remove_path(path_to_remove)
                connection.execute("DELETE FROM files WHERE file_ref=?", (file_ref,))
            else:
                connection.execute(
                    "UPDATE files SET active_readers=? WHERE file_ref=?", (readers, file_ref)
                )
        if path_to_remove is not None:
            self._remove_path(path_to_remove)

    def release_file(self, session_id: str, file_ref: str) -> dict[str, Any]:
        self._validate_ref(file_ref, _FILE_REF_RE, "file")
        path_to_remove: Path | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM files WHERE file_ref=? AND session_id=?",
                (file_ref, self._session_id(session_id)),
            ).fetchone()
            if row is None:
                return {"session_id": session_id, "file_ref": file_ref, "released": True}
            readers = int(row["active_readers"])
            active_tasks = int(row["active_tasks"])
            if readers or active_tasks:
                connection.execute("UPDATE files SET state='RELEASED' WHERE file_ref=?", (file_ref,))
                pending = True
            else:
                path_to_remove = self._path(file_ref, row["filename"])
                self._remove_path(path_to_remove)
                connection.execute("DELETE FROM files WHERE file_ref=?", (file_ref,))
                pending = False
            connection.execute(
                "UPDATE file_transfers SET state='RELEASED' WHERE file_ref=? AND state='READY'",
                (file_ref,),
            )
        return {"session_id": session_id, "file_ref": file_ref, "released": True, "pending_readers": pending}

    def cancel_transfer(self, session_id: str, transfer_id: str) -> dict[str, Any]:
        self._validate_ref(transfer_id, _TRANSFER_ID_RE, "transfer")
        path_to_remove: Path | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(connection, transfer_id, session_id)
            if row["state"] == "READY":
                raise FileTransferError("TRANSFER_READY", "release the file_ref after its active tasks finish", 409)
            if row["state"] not in {"CANCELLED", "EXPIRED"}:
                connection.execute(
                    "UPDATE file_transfers SET state='CANCELLED',error_code=NULL WHERE transfer_id=?",
                    (transfer_id,),
                )
                path_to_remove = self._path(row["file_ref"], row["filename"])
            result = self._transfer_row(connection, transfer_id, session_id)
            public = self._transfer_public(result)
        if path_to_remove is not None:
            self._remove_path(path_to_remove)
        return public

    def recover(
        self,
        active_task_ids: set[str] | None = None,
        *,
        recover_downloads: bool = False,
    ) -> dict[str, int]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale_refs = connection.execute("SELECT task_id,file_ref FROM file_task_refs").fetchall()
            if active_task_ids is not None:
                for ref in stale_refs:
                    if ref["task_id"] not in active_task_ids:
                        connection.execute(
                            "DELETE FROM file_task_refs WHERE task_id=? AND file_ref=?",
                            (ref["task_id"], ref["file_ref"]),
                        )
                connection.execute(
                    "UPDATE files SET active_tasks=(SELECT count(*) FROM file_task_refs r WHERE r.file_ref=files.file_ref)"
                )
            expired_uploads = connection.execute(
                "SELECT file_ref FROM file_transfers WHERE state IN ('RECEIVING','VERIFYING','FETCHING') AND expires_at<=?",
                (now,),
            ).fetchall()
            interrupted_downloads = []
            if recover_downloads:
                interrupted_downloads = connection.execute(
                    "SELECT file_ref,filename FROM file_transfers WHERE state='FETCHING'"
                ).fetchall()
                for row in interrupted_downloads:
                    self._remove_path(self._path(row["file_ref"], row["filename"]))
                connection.execute(
                    "UPDATE file_transfers SET state='FAILED',error_code='SOURCE_REFRESH_REQUIRED' WHERE state='FETCHING'"
                )
            connection.execute(
                "UPDATE file_transfers SET state='EXPIRED',error_code='TRANSFER_EXPIRED' WHERE state IN ('RECEIVING','VERIFYING','FETCHING') AND expires_at<=?",
                (now,),
            )
            rows = connection.execute(
                "SELECT * FROM files WHERE (state='READY' AND expires_at<=? OR state='RELEASED') AND active_readers=0 AND active_tasks=0",
                (now,),
            ).fetchall()
            for row in [*expired_uploads, *rows]:
                self._remove_path(self._path(row["file_ref"], row["filename"]))
            connection.executemany("DELETE FROM files WHERE file_ref=?", [(row["file_ref"],) for row in rows])
            cutoff = now - self.metadata_retention_seconds
            connection.execute(
                "UPDATE file_transfers SET state='EXPIRED',error_code='TRANSFER_EXPIRED' WHERE transfer_id IN (SELECT transfer_id FROM files WHERE expires_at<=?)",
                (now,),
            )
            connection.execute(
                "DELETE FROM file_transfers WHERE state IN ('CANCELLED','FAILED','EXPIRED') AND created_at<?",
                (cutoff,),
            )
        removed = len(expired_uploads) + len(rows) + len(interrupted_downloads)
        self._recover_verifying()
        return {"expired_files": removed}

    def _recover_verifying(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT transfer_id,session_id FROM file_transfers WHERE state='VERIFYING' AND expires_at>?",
                (time.time(),),
            ).fetchall()
        for row in rows:
            try:
                self.verify_upload(row["transfer_id"], row["session_id"])
            except FileTransferError:
                continue

    def _expire_rows(self, connection: sqlite3.Connection, now: float) -> None:
        expired_uploads = connection.execute(
            "SELECT file_ref FROM file_transfers WHERE state IN ('RECEIVING','VERIFYING','FETCHING') AND expires_at<=?",
            (now,),
        ).fetchall()
        connection.execute(
            "UPDATE file_transfers SET state='EXPIRED',error_code='TRANSFER_EXPIRED' WHERE state IN ('RECEIVING','VERIFYING','FETCHING') AND expires_at<=?",
            (now,),
        )
        expired_files = connection.execute(
            "SELECT file_ref FROM files WHERE (state='READY' AND expires_at<=? OR state='RELEASED') AND active_readers=0 AND active_tasks=0",
            (now,),
        ).fetchall()
        for row in [*expired_uploads, *expired_files]:
            self._remove_path(self._path(row["file_ref"], row["filename"]))
        connection.execute(
            "UPDATE file_transfers SET state='EXPIRED',error_code='TRANSFER_EXPIRED' WHERE transfer_id IN (SELECT transfer_id FROM files WHERE expires_at<=?)",
            (now,),
        )
        connection.executemany("DELETE FROM files WHERE file_ref=?", [(row["file_ref"],) for row in expired_files])
