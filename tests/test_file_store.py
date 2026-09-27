from __future__ import annotations

import hashlib
import multiprocessing
from pathlib import Path

import pytest

from chatgpt_oracle.file_store import FileStore, FileTransferError


def make_store(root: Path, *, max_file_bytes: int = 1 << 20, max_total_bytes: int = 2 << 20) -> FileStore:
    return FileStore(
        root,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_transfers=4,
        max_chunk_bytes=256,
        io_chunk_bytes=17,
        ttl_seconds=3600,
        metadata_retention_seconds=86400,
    )


def _append_upload_chunk_in_child(root: str, transfer_id: str, data: bytes, output) -> None:
    store = make_store(Path(root))
    try:
        result = store.append_chunk(transfer_id, "agent-a", 0, data)
        output.put(("ready", result["committed_offset"]))
    except FileTransferError as exc:
        output.put(("error", exc.code))


def test_resumable_upload_survives_store_recreation_and_verifies_digest(tmp_path: Path) -> None:
    content = bytes(range(251)) * 3
    digest = hashlib.sha256(content).hexdigest()
    store = make_store(tmp_path)
    transfer = store.create_upload(
        session_id="agent-a",
        filename="notes.bin",
        size=len(content),
        mime_type="application/octet-stream",
        sha256=digest,
        request_id="upload-1",
    )
    assert transfer["state"] == "RECEIVING"
    assert store.create_upload(
        session_id="agent-a",
        filename="notes.bin",
        size=len(content),
        mime_type="application/octet-stream",
        sha256=digest,
        request_id="upload-1",
    )["transfer_id"] == transfer["transfer_id"]

    reopened = make_store(tmp_path)
    for offset in range(0, len(content), 173):
        part = content[offset : offset + 173]
        status = reopened.append_chunk(transfer["transfer_id"], "agent-a", offset, part)

    assert status["state"] == "READY"
    assert status["committed_offset"] == len(content)
    assert status["sha256"] == digest
    record = reopened.get_file("agent-a", transfer["file_ref"])
    assert record.size == len(content)
    assert reopened._path(record.file_ref).read_bytes() == content
    assert reopened.transfer_status(transfer["transfer_id"], "agent-a")["state"] == "READY"


def test_offset_session_and_checksum_errors_do_not_publish_file(tmp_path: Path) -> None:
    content = b"expected bytes"
    store = make_store(tmp_path)
    transfer = store.create_upload(
        session_id="agent-a",
        filename="notes.txt",
        size=len(content),
        mime_type="text/plain",
        sha256="0" * 64,
    )

    with pytest.raises(FileTransferError, match="expected Upload-Offset") as offset_error:
        store.append_chunk(transfer["transfer_id"], "agent-a", 1, content[:2])
    assert offset_error.value.status_code == 409

    with pytest.raises(FileTransferError) as session_error:
        store.transfer_status(transfer["transfer_id"], "agent-b")
    assert session_error.value.status_code == 404

    with pytest.raises(FileTransferError, match="SHA-256") as digest_error:
        store.append_chunk(transfer["transfer_id"], "agent-a", 0, content)
    assert digest_error.value.code == "CHECKSUM_MISMATCH"
    with pytest.raises(FileTransferError) as unpublished:
        store.get_file("agent-a", transfer["file_ref"])
    assert unpublished.value.status_code == 404


def test_failed_disk_write_does_not_commit_offset_and_can_retry(tmp_path: Path, monkeypatch) -> None:
    content = b"retriable upload block"
    store = make_store(tmp_path)
    transfer = store.create_upload(
        session_id="agent-a",
        filename="retry.bin",
        size=len(content),
        mime_type="application/octet-stream",
        sha256=hashlib.sha256(content).hexdigest(),
    )
    file_path = store._path(transfer["file_ref"])
    original_open = Path.open

    class NoSpaceFile:
        def __init__(self, path: Path, mode: str, *args: object, **kwargs: object):
            self._file = original_open(path, mode, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            self._file.close()

        def write(self, data: bytes) -> int:
            self._file.write(data[: max(1, len(data) // 2)])
            raise OSError(28, "simulated disk full")

        def __getattr__(self, name: str):
            return getattr(self._file, name)

    def fail_target_write(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path == file_path and mode == "r+b":
            return NoSpaceFile(path, mode, *args, **kwargs)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_target_write)
    with pytest.raises(OSError, match="simulated disk full"):
        store.append_chunk(transfer["transfer_id"], "agent-a", 0, content)
    monkeypatch.setattr(Path, "open", original_open)

    status = store.transfer_status(transfer["transfer_id"], "agent-a")
    assert status["committed_offset"] == 0
    assert status["state"] == "RECEIVING"
    assert file_path.stat().st_size < len(content)

    completed = store.append_chunk(transfer["transfer_id"], "agent-a", 0, content)
    assert completed["state"] == "READY"
    assert completed["committed_offset"] == len(content)
    assert completed["sha256"] == hashlib.sha256(content).hexdigest()


def test_zero_byte_file_and_idempotency_conflict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    empty_digest = hashlib.sha256(b"").hexdigest()
    transfer = store.create_upload(
        session_id="agent-a",
        filename="empty.bin",
        size=0,
        mime_type="application/octet-stream",
        sha256=empty_digest,
        request_id="empty-1",
    )
    assert transfer["state"] == "READY"
    assert store.get_file("agent-a", transfer["file_ref"]).size == 0

    with pytest.raises(FileTransferError) as conflict:
        store.create_upload(
            session_id="agent-a",
            filename="different.bin",
            size=0,
            mime_type="application/octet-stream",
            sha256=empty_digest,
            request_id="empty-1",
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_release_waits_for_active_reader_and_is_scoped(tmp_path: Path) -> None:
    data = b"read lease"
    store = make_store(tmp_path)
    transfer = store.create_upload(
        session_id="agent-a",
        filename="notes.txt",
        size=len(data),
        mime_type="text/plain",
        sha256=hashlib.sha256(data).hexdigest(),
    )
    store.append_chunk(transfer["transfer_id"], "agent-a", 0, data)

    record, path = store.read_path("agent-a", transfer["file_ref"])
    assert path.read_bytes() == data
    assert store.release_file("agent-a", record.file_ref)["pending_readers"] is True
    assert path.exists()
    with pytest.raises(FileTransferError):
        store.get_file("agent-a", record.file_ref)

    store.finish_read(record.file_ref)
    assert not path.exists()
    assert store.release_file("agent-a", record.file_ref)["released"] is True


def test_upload_limits_and_cancellation_cleanup(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_file_bytes=32, max_total_bytes=48)
    with pytest.raises(FileTransferError) as too_large:
        store.create_upload(session_id="agent-a", filename="big.bin", size=33, mime_type=None)
    assert too_large.value.status_code == 413

    transfer = store.create_upload(session_id="agent-a", filename="partial.bin", size=32, mime_type=None)
    store.append_chunk(transfer["transfer_id"], "agent-a", 0, b"part")
    file_path = store._path(transfer["file_ref"])
    assert file_path.exists()
    assert store.cancel_transfer("agent-a", transfer["transfer_id"])["state"] == "CANCELLED"
    assert not file_path.exists()


def test_task_file_pins_survive_session_release_until_job_finishes(tmp_path: Path) -> None:
    content = b"keep pinned until send finishes"
    store = make_store(tmp_path)
    transfer = store.create_upload(
        session_id="agent-a",
        filename="research-notes.txt",
        size=len(content),
        mime_type="text/plain",
        sha256=hashlib.sha256(content).hexdigest(),
    )
    store.append_chunk(transfer["transfer_id"], "agent-a", 0, content)
    store.attach_files_to_task("agent-a", "cgo_test", [transfer["file_ref"]])

    released = store.release_file("agent-a", transfer["file_ref"])
    assert released["pending_readers"] is True
    pinned = store.files_for_task("cgo_test")
    assert len(pinned) == 1
    assert pinned[0].path.name == "research-notes.txt"
    assert pinned[0].path.read_bytes() == content

    store.release_task_files("cgo_test")
    assert store.files_for_task("cgo_test") == []
    assert not pinned[0].path.exists()


def test_interrupted_source_download_needs_a_fresh_source_after_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    transfer = store.create_download(
        session_id="agent-a",
        filename="image.png",
        mime_type="image/png",
        source_identity="exp_1:file_1",
        file_kind="image_asset_pointer",
        request_id="download-1",
    )
    path = store.download_path(transfer["transfer_id"], "agent-a")
    path.write_bytes(b"partial source bytes")
    store.update_download_progress(transfer["transfer_id"], "agent-a", len(b"partial source bytes"))

    recovered = make_store(tmp_path)
    recovered.recover(recover_downloads=True)
    status = recovered.transfer_status(transfer["transfer_id"], "agent-a")
    assert status["state"] == "FAILED"
    assert status["error_code"] == "SOURCE_REFRESH_REQUIRED"
    assert not path.exists()


def test_multiprocess_upload_writers_serialize_on_committed_offset(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    chunk = b"x" * 256
    transfer = store.create_upload(
        session_id="agent-a",
        filename="race.bin",
        size=len(chunk) * 2,
        mime_type="application/octet-stream",
    )
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_append_upload_chunk_in_child,
            args=(str(tmp_path), transfer["transfer_id"], chunk, output),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        outcomes = {output.get(timeout=2) for _ in processes}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        output.close()
        output.join_thread()

    assert outcomes == {("ready", len(chunk)), ("error", "OFFSET_MISMATCH")}
    status = store.transfer_status(transfer["transfer_id"], "agent-a")
    assert status["committed_offset"] == len(chunk)
