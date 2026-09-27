from __future__ import annotations

import hashlib
import json

import pytest

from chatgpt_oracle.exports import (
    ExportCapacityError,
    ExportSessionMismatch,
    ExportStore,
    InvalidExportCursor,
)


def _payload(text: str, attachment_id: str = "att-1") -> dict[str, object]:
    message = {
        "id": "message-1",
        "role": "assistant",
        "ordinal": 0,
        "text": text,
        "canvas": {"text": "diagram", "source_url": "https://oaiusercontent.com/file?sig=private"},
        "attachments": [{"attachment_id": attachment_id, "filename": "report.zip"}],
    }
    attachment = {
        "attachment_id": attachment_id,
        "message_id": "message-1",
        "filename": "report.zip",
        "source_url": "https://chatgpt.com/backend-api/files/download/file-1?conversation_id=c-1",
        "download_url": "/exports/download/dl-legacy",
    }
    return {
        "manifest": {"messages": [message], "attachments": [attachment]},
        "attachments": [attachment],
        "markdown": f"## Assistant\n\n{text}\n",
    }


def _collect(store: ExportStore, session_id: str, export_id: str, view: str) -> bytes:
    cursor: str | None = None
    chunks: list[bytes] = []
    while True:
        page = store.page(
            session_id=session_id,
            export_id=export_id,
            view=view,
            cursor=cursor,
            max_response_bytes=2048,
        )
        chunks.append(page["chunk"].encode("utf-8"))
        cursor = page["next_cursor"]
        if page["eof"]:
            assert cursor is None
            return b"".join(chunks)


def _collect_messages(
    store: ExportStore,
    session_id: str,
    export_id: str,
    *,
    max_response_bytes: int = 2048,
) -> tuple[list[dict[str, object]], int]:
    cursor: str | None = None
    records: dict[int, dict[str, object]] = {}
    segments: dict[int, bytearray] = {}
    pages = 0
    while True:
        page = store.page(
            session_id=session_id,
            export_id=export_id,
            view="messages",
            cursor=cursor,
            max_response_bytes=max_response_bytes,
        )
        encoded_size = len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert encoded_size <= max_response_bytes
        pages += 1
        segment = page.get("message_segment")
        if isinstance(segment, dict):
            ordinal = int(segment["ordinal"])
            piece = str(segment["chunk"]).encode("utf-8")
            assert segment["byte_offset"] == len(segments.setdefault(ordinal, bytearray()))
            assert segment["returned_bytes"] == len(piece)
            assert hashlib.sha256(piece).hexdigest() == segment["chunk_sha256"]
            segments[ordinal].extend(piece)
            if segment["complete"]:
                complete = bytes(segments[ordinal])
                assert len(complete) == segment["total_bytes"]
                assert hashlib.sha256(complete).hexdigest() == segment["sha256"]
                records[ordinal] = json.loads(complete)
        else:
            for message in page["messages"]:
                records[int(message["ordinal"])] = message

        cursor = page["next_cursor"]
        if page["eof"]:
            assert cursor is None
            break
        assert cursor

    assert sorted(records) == list(range(page["message_count"]))
    return [records[index] for index in sorted(records)], pages


def test_paged_views_reassemble_exactly_and_strip_attachment_urls() -> None:
    text = "中文🙂\n" + ("长消息 content " * 3000)
    payload = _payload(text)
    store = ExportStore(ttl_seconds=60)
    status = store.put_snapshot(
        "exp-paged",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation-a",
        payload=payload,
        source="dom",
        completeness={
            "messages": "unverified",
            "attachments": "unverified",
            "overall": "unverified",
        },
    )

    message_records, page_count = _collect_messages(store, "agent-a", "exp-paged")
    messages = json.dumps(
        message_records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    markdown = _collect(store, "agent-a", "exp-paged", "markdown")
    attachments = _collect(store, "agent-a", "exp-paged", "attachments")
    message_records = json.loads(messages)
    attachment_records = json.loads(attachments)
    assert message_records[0]["ordinal"] == 0
    assert message_records[0]["text"] == text
    assert message_records[0]["attachments"][0]["filename"] == "report.zip"
    assert markdown == payload["markdown"].encode("utf-8")
    assert attachment_records[0]["message_ordinal"] == 0
    assert attachment_records[0]["available"] is True
    assert "source_url" not in attachment_records[0]
    assert "download_url" not in attachment_records[0]
    assert b"source_url" not in messages
    assert b"sig=private" not in messages
    assert hashlib.sha256(messages).hexdigest() == status["views"]["messages"]["sha256"]
    assert hashlib.sha256(attachments).hexdigest() == status["views"]["attachments"]["sha256"]
    assert page_count > 1


def test_cursor_is_repeatable_and_bound_to_session_export_and_view() -> None:
    store = ExportStore(ttl_seconds=60)
    store.put_snapshot(
        "exp-bound",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation-a",
        payload=_payload("x" * 5000),
        source="api",
        completeness={
            "messages": "verified",
            "attachments": "verified",
            "overall": "verified",
        },
    )
    first = store.page(
        session_id="agent-a",
        export_id="exp-bound",
        view="messages",
        cursor=None,
        max_response_bytes=2048,
    )
    retry = store.page(
        session_id="agent-a",
        export_id="exp-bound",
        view="messages",
        cursor=None,
        max_response_bytes=2048,
    )
    for key in ("messages", "message_segment", "start_ordinal", "next_cursor", "eof", "sha256"):
        assert first[key] == retry[key]

    with pytest.raises(ExportSessionMismatch):
        store.page(
            session_id="agent-b",
            export_id="exp-bound",
            view="messages",
            cursor=None,
            max_response_bytes=2048,
        )
    with pytest.raises(InvalidExportCursor):
        store.page(
            session_id="agent-a",
            export_id="exp-bound",
            view="markdown",
            cursor=first["next_cursor"],
            max_response_bytes=2048,
        )


def test_long_message_segments_are_structured_and_bounded() -> None:
    payload = _payload("🙂" * 10000)
    store = ExportStore(ttl_seconds=60)
    status = store.put_snapshot(
        "exp-long-message",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation-a",
        payload=payload,
        source="api",
        completeness={"messages": "verified", "attachments": "unverified"},
    )

    records, page_count = _collect_messages(
        store,
        "agent-a",
        "exp-long-message",
        max_response_bytes=2048,
    )
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert records[0]["text"] == "🙂" * 10000
    assert hashlib.sha256(encoded).hexdigest() == status["views"]["messages"]["sha256"]
    assert page_count > 1


def test_page_payload_obeys_limit_and_cursor_rejects_tampering() -> None:
    store = ExportStore(ttl_seconds=60)
    store.put_snapshot(
        "exp-limit",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation-a",
        payload=_payload("🙂" * 10000),
        source="dom",
        completeness={
            "messages": "unverified",
            "attachments": "unverified",
            "overall": "unverified",
        },
    )
    page = store.page(
        session_id="agent-a",
        export_id="exp-limit",
        view="markdown",
        cursor=None,
        max_response_bytes=2048,
    )
    encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 2048
    assert page["chunk"].encode("utf-8").decode("utf-8") == page["chunk"]
    assert page["next_cursor"]

    token = page["next_cursor"]
    changed = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(InvalidExportCursor):
        store.page(
            session_id="agent-a",
            export_id="exp-limit",
            view="markdown",
            cursor=changed,
            max_response_bytes=2048,
        )


def test_release_is_terminal_and_revokes_attachment_sources() -> None:
    store = ExportStore(ttl_seconds=60)
    store.put_snapshot(
        "exp-release",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation-a",
        payload=_payload("text"),
        source="api",
        completeness={
            "messages": "verified",
            "attachments": "verified",
            "overall": "verified",
        },
    )
    store.release_snapshot("agent-a", "exp-release")
    with pytest.raises(KeyError):
        store.page(
            session_id="agent-a",
            export_id="exp-release",
            view="messages",
            cursor=None,
            max_response_bytes=2048,
        )


def test_snapshot_views_are_disk_backed_and_release_waits_for_active_reader(tmp_path) -> None:
    store = ExportStore(ttl_seconds=60, snapshot_dir=tmp_path / "snapshots")
    payload = _payload("disk-backed export")
    status = store.put_snapshot(
        "exp-disk",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation-a",
        payload=payload,
        source="fixture",
        completeness={"messages": "verified", "attachments": "verified"},
    )
    snapshot = store._snapshots["exp-disk"]
    assert not hasattr(snapshot, "views")
    assert all(path.is_file() for path in snapshot.view_paths.values())
    assert store.view_bytes("agent-a", "exp-disk", "messages")
    assert status["views"]["messages"]["total_bytes"] == snapshot.view_sizes["messages"]

    lease = store.view_file("agent-a", "exp-disk", "markdown")
    store.release_snapshot("agent-a", "exp-disk")
    assert lease.path.is_file()
    store.release_view_file(lease)
    assert not snapshot.storage_dir.exists()
    store.close()


def test_snapshot_capacity_failure_removes_staging_files(tmp_path) -> None:
    store = ExportStore(ttl_seconds=60, max_snapshot_bytes=128, snapshot_dir=tmp_path / "snapshots")
    with pytest.raises(ExportCapacityError):
        store.put_snapshot(
            "exp-too-large",
            session_id="agent-a",
            conversation_url="https://chatgpt.com/c/conversation-a",
            payload=_payload("too large"),
            source="fixture",
            completeness={"messages": "verified", "attachments": "verified"},
        )

    assert list(store._storage_root.iterdir()) == []
    store.close()
