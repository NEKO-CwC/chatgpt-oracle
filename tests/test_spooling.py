from __future__ import annotations

import hashlib
import json
import os
import tracemalloc

import pytest

from chatgpt_oracle.browser import ChatGPTBrowser
from chatgpt_oracle.config import Settings
from chatgpt_oracle.exports import ExportCapacityError, ExportStore, make_export
from chatgpt_oracle.spooling import PagedMessageSpool


def _page(messages: list[dict[str, object]], *, previous: bool, next_page: bool, attachments=None):
    payload = {
        "messages": messages,
        "page_info": {
            "has_previous_page": previous,
            "has_next_page": next_page,
        },
    }
    if attachments is not None:
        payload["attachments"] = attachments
    normalized = {
        "messages": [
            ChatGPTBrowser._normalise_api_message(message, index)
            for index, message in enumerate(messages)
        ],
        "attachments": attachments if attachments is not None else [
            {**attachment, "message_id": attachment.get("message_id") or message["id"]}
            for message in [item for item in ChatGPTBrowser._normalise_api_conversation(payload, "https://chatgpt.com/c/test")["messages"]]
            for attachment in message.get("attachments", [])
        ],
        "excluded_system_message_count": 0,
    }
    return payload, normalized


def test_page_spool_reorders_pages_deduplicates_and_keeps_conflict_partial(tmp_path) -> None:
    older_payload, older_normalized = _page(
        [
            {"id": "m1", "author": {"role": "user"}, "content": {"parts": ["first"]}},
            {"id": "m2", "author": {"role": "assistant"}, "content": {"parts": ["same"]}},
        ],
        previous=False,
        next_page=True,
        attachments=[{
            "attachment_id": "file_1",
            "file_id": "file_1",
            "message_id": "m1",
            "source_url": "https://chatgpt.com/backend-api/files/download/file_1?sig=private",
        }],
    )
    latest_payload, latest_normalized = _page(
        [
            {"id": "m2", "author": {"role": "assistant"}, "content": {"parts": ["same"]}},
            {"id": "m3", "author": {"role": "assistant"}, "content": {"parts": ["last"]}},
        ],
        previous=True,
        next_page=False,
    )

    spool = PagedMessageSpool(tmp_path / "snapshots", max_bytes=1024 * 1024)
    spool.append_page(latest_payload, latest_normalized, cursor_matches=True)
    spool.append_page(older_payload, older_normalized, cursor_matches=True)
    summary = spool.finalize()

    assert [message["id"] for message in spool] == ["m1", "m2", "m3"]
    assert summary["pagination"]["page_count"] == 2
    assert summary["pagination"]["raw_message_count"] == 4
    assert summary["pagination"]["duplicate_count"] == 1
    assert summary["pagination"]["conflicting_duplicate"] is False
    assert summary["pagination"]["completeness"] == "verified"
    assert summary["attachments"][0]["attachment_id"] == "file_1"
    assert all(b"sig=private" not in page.path.read_bytes() for page in spool._pages)
    spool.close()

    conflicting_latest_payload, conflicting_latest_normalized = _page(
        [
            {"id": "m2", "author": {"role": "assistant"}, "content": {"parts": ["changed"]}},
            {"id": "m3", "author": {"role": "assistant"}, "content": {"parts": ["last"]}},
        ],
        previous=True,
        next_page=False,
    )
    conflict_spool = PagedMessageSpool(tmp_path / "snapshots", max_bytes=1024 * 1024)
    conflict_spool.append_page(conflicting_latest_payload, conflicting_latest_normalized, cursor_matches=True)
    conflict_spool.append_page(older_payload, older_normalized, cursor_matches=True)
    conflict_summary = conflict_spool.finalize()
    assert conflict_summary["pagination"]["conflicting_duplicate"] is True
    assert conflict_summary["pagination"]["completeness"] == "partial"
    conflict_spool.close()


def test_spooled_make_export_matches_list_views_and_pages(tmp_path) -> None:
    messages = [
        {
            "id": "u1",
            "author": {"role": "user"},
            "content": {"parts": ["ask"]},
            "attachments": [{
                "id": "file_1",
                "attachment_id": "file_1",
                "file_id": "file_1",
                "filename": "input.txt",
                "mime_type": "text/plain",
                "size": 3,
                "source_url": "https://chatgpt.com/backend-api/files/download/file_1?conversation_id=spooled",
            }],
        },
        {"id": "a1", "author": {"role": "assistant"}, "content": {"parts": ["answer"]}},
    ]
    payload, normalized = _page(messages, previous=False, next_page=False)
    spool = PagedMessageSpool(tmp_path / "snapshots", max_bytes=1024 * 1024)
    spool.append_page(payload, normalized, cursor_matches=True)
    summary = spool.finalize()
    browser_payload = {
        "messages": spool,
        "attachments": summary["attachments"],
        "markdown": "",
        "source": "api_paged",
    }
    streamed = make_export(
        export_id="exp-spooled",
        conversation_url="https://chatgpt.com/c/spooled",
        scope="all",
        count=10,
        browser_payload=browser_payload,
        handles=ExportStore(ttl_seconds=60),
        create_download_handles=False,
    )
    ordinary = make_export(
        export_id="exp-list",
        conversation_url="https://chatgpt.com/c/spooled",
        scope="all",
        count=10,
        browser_payload={"messages": normalized["messages"], "attachments": normalized["attachments"]},
        handles=ExportStore(ttl_seconds=60),
        create_download_handles=False,
    )
    streamed["markdown"] = "".join(streamed["markdown"])
    assert streamed["markdown"] == ordinary["markdown"]
    assert streamed["attachments"] == ordinary["attachments"]

    store = ExportStore(ttl_seconds=60, snapshot_dir=tmp_path / "snapshots")
    ordinary_store = ExportStore(ttl_seconds=60)
    status = store.put_snapshot(
        "exp-spooled",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/spooled",
        payload=streamed,
        source="api_paged",
        completeness={"messages": "verified", "attachments": "verified"},
    )
    ordinary_store.put_snapshot(
        "exp-list",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/spooled",
        payload=ordinary,
        source="fixture",
        completeness={"messages": "verified", "attachments": "verified"},
    )
    stored_bytes = store.view_bytes("agent-a", "exp-spooled", "messages")
    ordinary_bytes = ordinary_store.view_bytes("agent-a", "exp-list", "messages")
    assert stored_bytes == ordinary_bytes
    stored_messages = json.loads(stored_bytes)
    assert [message["id"] for message in stored_messages] == ["u1", "a1"]
    for view in ("messages", "markdown", "attachments"):
        assert store.view_bytes("agent-a", "exp-spooled", view) == ordinary_store.view_bytes("agent-a", "exp-list", view)
        assert hashlib.sha256(store.view_bytes("agent-a", "exp-spooled", view)).hexdigest() == status["views"][view]["sha256"]
    store.close()
    ordinary_store.close()
    spool.close()


def test_page_spool_rejects_capacity_without_publishing_messages(tmp_path) -> None:
    payload, normalized = _page(
        [{"id": "large", "author": {"role": "user"}, "content": {"parts": ["x" * 2048]}}],
        previous=False,
        next_page=False,
    )
    spool = PagedMessageSpool(tmp_path / "snapshots", max_bytes=128)
    with pytest.raises(ExportCapacityError):
        spool.append_page(payload, normalized, cursor_matches=True)
    spool.close()
    assert not list((tmp_path / "snapshots").glob("ingest-*"))


def test_export_store_removes_dead_ingest_spools_but_preserves_live_ones(tmp_path) -> None:
    root = tmp_path / "snapshots"
    stale = root / "ingest-2147483647-orphan"
    stale.mkdir(parents=True)
    (stale / "message.jsonl").write_text("temporary", encoding="utf-8")

    live = PagedMessageSpool(root, max_bytes=1024)
    store = ExportStore(ttl_seconds=60, snapshot_dir=root)

    assert not stale.exists()
    assert live._root.exists()
    store.close()
    live.close()
    assert not live._root.exists()


@pytest.mark.skipif(
    os.environ.get("CHATGPT_ORACLE_RUN_PRESSURE_TESTS") != "1",
    reason="set CHATGPT_ORACLE_RUN_PRESSURE_TESTS=1 for the large export pressure test",
)
def test_large_single_message_is_spooled_snapshotted_and_paged_to_eof(tmp_path) -> None:
    message_bytes = 16 * 1024 * 1024
    text = "x" * message_bytes
    payload = {
        "messages": [{
            "id": "oversized-message",
            "author": {"role": "assistant"},
            "content": {"parts": [text]},
        }],
        "page_info": {"has_previous_page": False, "has_next_page": False},
    }
    root = tmp_path / "snapshots"
    store = ExportStore(
        ttl_seconds=60,
        max_snapshot_bytes=64 * 1024 * 1024,
        snapshot_dir=root,
    )
    spool = PagedMessageSpool(root, max_bytes=64 * 1024 * 1024)
    tracemalloc.start()
    try:
        normalized = ChatGPTBrowser._normalise_api_conversation(
            payload,
            "https://chatgpt.com/c/large-message",
        )
        source_page_bytes = spool.measure_page(payload)
        spool.append_page(
            payload,
            normalized,
            cursor_matches=True,
            source_page_bytes=source_page_bytes,
        )
        summary = spool.finalize()
        manifest = make_export(
            export_id="exp-large-message",
            conversation_url="https://chatgpt.com/c/large-message",
            scope="all",
            count=10,
            browser_payload={
                "messages": spool,
                "attachments": summary["attachments"],
                "markdown": "",
                "source": "api_paged",
            },
            handles=store,
            create_download_handles=False,
        )
        status = store.put_snapshot(
            "exp-large-message",
            session_id="agent-a",
            conversation_url="https://chatgpt.com/c/large-message",
            payload=manifest,
            source="api_paged",
            completeness={"messages": "verified", "attachments": "verified"},
        )
        snapshot = store._snapshots["exp-large-message"]
        snapshot_bytes = sum(snapshot.view_sizes.values())
        peak_disk_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        assert status["message_count"] == 1
        assert spool.staged_bytes > message_bytes
        assert snapshot_bytes > message_bytes * 2
        assert peak_disk_bytes == spool.staged_bytes + snapshot_bytes

        cursor = None
        offset = 0
        reconstructed = hashlib.sha256()
        expected_sha256 = None
        while True:
            page = store.page(
                session_id="agent-a",
                export_id="exp-large-message",
                view="messages",
                cursor=cursor,
                max_response_bytes=15_360,
            )
            segment = page["message_segment"]
            assert segment is not None
            assert segment["byte_offset"] == offset
            assert segment["message_id"] == "oversized-message"
            expected_sha256 = expected_sha256 or segment["sha256"]
            chunk = segment["chunk"].encode("utf-8")
            assert len(chunk) == segment["returned_bytes"]
            reconstructed.update(chunk)
            offset += len(chunk)
            assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 15_360
            if page["eof"]:
                break
            cursor = page["next_cursor"]
            assert cursor

        assert offset == segment["total_bytes"]
        assert reconstructed.hexdigest() == expected_sha256
        assert segment["complete"] is True
        _, peak_python_bytes = tracemalloc.get_traced_memory()
        print(
            "large export pressure: "
            f"source_page={source_page_bytes} spool={spool.staged_bytes} "
            f"snapshot={snapshot_bytes} peak_disk={peak_disk_bytes} "
            f"peak_python_allocations={peak_python_bytes}"
        )

        spool.close()
        assert sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) == snapshot_bytes
        store.release_snapshot("agent-a", "exp-large-message")
        assert sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) == 0
    finally:
        tracemalloc.stop()
        spool.close()
        store.close()


@pytest.mark.skipif(
    os.environ.get("CHATGPT_ORACLE_RUN_PRESSURE_TESTS") != "1",
    reason="set CHATGPT_ORACLE_RUN_PRESSURE_TESTS=1 for the oversized export capacity test",
)
def test_single_message_over_snapshot_quota_fails_without_publishing_partial_views(tmp_path) -> None:
    text = "x" * (24 * 1024 * 1024)
    payload = {
        "messages": [{
            "id": "over-capacity-message",
            "author": {"role": "assistant"},
            "content": {"parts": [text]},
        }],
        "page_info": {"has_previous_page": False, "has_next_page": False},
    }
    root = tmp_path / "snapshots"
    store = ExportStore(
        ttl_seconds=60,
        max_snapshot_bytes=64 * 1024 * 1024,
        snapshot_dir=root,
    )
    spool = PagedMessageSpool(root, max_bytes=64 * 1024 * 1024)
    try:
        normalized = ChatGPTBrowser._normalise_api_conversation(
            payload,
            "https://chatgpt.com/c/over-capacity",
        )
        spool.append_page(
            payload,
            normalized,
            cursor_matches=True,
            source_page_bytes=spool.measure_page(payload),
        )
        summary = spool.finalize()
        manifest = make_export(
            export_id="exp-over-capacity",
            conversation_url="https://chatgpt.com/c/over-capacity",
            scope="all",
            count=10,
            browser_payload={
                "messages": spool,
                "attachments": summary["attachments"],
                "markdown": "",
                "source": "api_paged",
            },
            handles=store,
            create_download_handles=False,
        )
        with pytest.raises(ExportCapacityError, match="snapshot storage limit"):
            store.put_snapshot(
                "exp-over-capacity",
                session_id="agent-a",
                conversation_url="https://chatgpt.com/c/over-capacity",
                payload=manifest,
                source="api_paged",
                completeness={"messages": "verified", "attachments": "verified"},
            )

        assert "exp-over-capacity" not in store._snapshots
        assert list(store._storage_root.iterdir()) == []
    finally:
        spool.close()
        store.close()
