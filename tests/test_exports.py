from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from chatgpt_oracle.config import Settings
from chatgpt_oracle.exports import ExportStore, make_export, recent_turns
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job


def test_recent_scope_counts_user_turns_and_preserves_order() -> None:
    messages = [
        {"id": "u1", "role": "user"},
        {"id": "a1", "role": "assistant"},
        {"id": "u2", "role": "user"},
        {"id": "a2", "role": "assistant"},
        {"id": "u3", "role": "user"},
        {"id": "a3", "role": "assistant"},
    ]
    assert [item["id"] for item in recent_turns(messages, 2)] == ["u2", "a2", "u3", "a3"]


def test_manifest_keeps_rich_content_and_removes_credential_keys() -> None:
    store = ExportStore(ttl_seconds=60)
    result = make_export(
        export_id="exp_test",
        conversation_url="https://chatgpt.com/c/test",
        scope="all",
        count=10,
        handles=store,
        browser_payload={
            "messages": [
                {
                    "id": "m1",
                    "role": "assistant",
                    "text": "Answer mentions CAPTCHA as ordinary text.",
                    "session_token": "must-not-leak",
                    "canvas": {"kind": "text_document"},
                    "deep_research": {"summary": "research summary", "citations": [{"title": "Source", "url": "https://example.com/source"}]},
                    "rich_parts": [{"type": "math", "latex": "x^2"}],
                    "code_blocks": [{"language": "python", "text": "print('ok')"}],
                    "tables": [{"text": "a | b"}],
                    "attachments": [
                        {"filename": "same.txt", "source_url": "https://chatgpt.com/files/1"},
                        {"filename": "same.txt", "source_url": "https://chatgpt.com/files/2"},
                    ],
                }
            ],
            "authorization": "must-not-leak",
        },
    )
    assert len(result["attachments"]) == 2
    assert result["manifest"]["messages"][0]["canvas"]["kind"] == "text_document"
    assert "print('ok')" in result["markdown"]
    assert "a | b" in result["markdown"]
    assert '"kind": "text_document"' in result["markdown"]
    assert '"summary": "research summary"' in result["markdown"]
    assert '"latex": "x^2"' in result["markdown"]
    assert "same.txt" in result["markdown"]
    assert "source_url" not in result["markdown"]
    encoded = repr(result).lower()
    assert "session_token" not in encoded
    assert "must-not-leak" not in encoded


def test_file_id_gets_only_an_unverified_card_action_candidate() -> None:
    store = ExportStore(ttl_seconds=60)
    result = make_export(
        export_id="exp_file",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=store,
        browser_payload={
            "messages": [{"id": "m", "role": "user", "text": "file"}],
            "attachments": [{
                "attachment_id": "file_123",
                "file_id": "file_123",
                "filename": "notes.txt",
                "message_id": "m",
            }],
        },
    )
    attachment = result["attachments"][0]
    assert "/backend-api/files/download/file_123" in attachment["source_url"]
    assert attachment["download_candidate"] is True
    assert attachment["downloadable"] is False

    store.put_snapshot(
        "exp_file",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation",
        payload=result,
        source="fixture",
        completeness={"messages": "verified", "attachments": "unverified", "overall": "unverified"},
    )
    public_attachment = json.loads(store.view_bytes("agent-a", "exp_file", "attachments"))[0]
    assert public_attachment["available"] is False
    assert public_attachment["download_candidate"] is True
    assert public_attachment["downloadable"] is False
    handle = store.attachment_handle("agent-a", "exp_file", "file_123")
    assert "/backend-api/files/download/file_123" in handle.source_url


def test_sediment_image_asset_pointer_without_download_evidence_stays_unavailable() -> None:
    store = ExportStore(ttl_seconds=60)
    result = make_export(
        export_id="exp_asset_pointer",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=store,
        browser_payload={
            "messages": [{
                "id": "m",
                "role": "assistant",
                "text": "generated image",
                "rich_parts": [{
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "sediment://file_123#thumbnail",
                    "size_bytes": 321,
                }],
            }],
            "attachments": [],
        },
        create_download_handles=False,
    )

    assert len(result["attachments"]) == 1
    attachment = result["attachments"][0]
    assert attachment["file_id"] == "file_123"
    assert attachment["file_kind"] == "image_asset_pointer"
    assert attachment["size"] is None
    assert attachment["download_candidate"] is False
    assert attachment["downloadable"] is False
    assert attachment["source_url"] is None
    assert attachment["download_url"] is None
    assert attachment["unavailable_reason"] == "ChatGPT asset pointer has no verified download source"


def test_pointer_match_takes_precedence_over_generic_attachment_reference_kind() -> None:
    result = make_export(
        export_id="exp_asset_pointer_attachment_ref",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=ExportStore(ttl_seconds=60),
        browser_payload={
            "messages": [{
                "id": "m",
                "role": "assistant",
                "rich_parts": [{
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "sediment://file_123#thumbnail",
                }],
            }],
            "attachments": [{
                "attachment_id": "file_123",
                "message_id": "m",
            }],
        },
        create_download_handles=False,
    )

    assert len(result["attachments"]) == 1
    attachment = result["attachments"][0]
    assert attachment["file_kind"] == "image_asset_pointer"
    assert attachment["file_id"] == "file_123"
    assert attachment["downloadable"] is False
    assert attachment["unavailable_reason"] == "ChatGPT asset pointer has no verified download source"


def test_observed_file_source_enriches_duplicate_asset_pointer_reference() -> None:
    store = ExportStore(ttl_seconds=60)
    source_url = (
        "https://chatgpt.com/backend-api/files/download/file_123"
        "?download_intent=false&inline=false"
    )
    result = make_export(
        export_id="exp_asset_source",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=store,
        browser_payload={
            "messages": [{
                "id": "m",
                "role": "assistant",
                "text": "generated image",
                "rich_parts": [{
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "sediment://file_123#thumbnail",
                    "size_bytes": 321,
                }],
            }],
            "attachments": [
                {
                    "attachment_id": "file_123",
                    "file_id": "file_123",
                    "message_id": "m",
                    "file_kind": "image_asset_pointer",
                    "size": 512134,
                },
                {
                    "attachment_id": "file_123",
                    "file_id": "file_123",
                    "message_id": "m",
                    "filename": "generated.png",
                    "mime_type": "image/png",
                    "size": 321,
                    "file_kind": "image_asset_pointer",
                    "source_url": source_url,
                    "downloadable": True,
                },
            ],
        },
        create_download_handles=False,
    )

    assert len(result["attachments"]) == 1
    attachment = result["attachments"][0]
    assert attachment["source_url"] == source_url
    assert attachment["downloadable"] is True
    assert attachment["download_candidate"] is False
    assert attachment["filename"] == "generated.png"
    assert attachment["size"] is None
    store.put_snapshot(
        "exp_asset_source",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/conversation",
        payload=result,
        source="fixture",
        completeness={"messages": "verified", "attachments": "partial", "overall": "partial"},
    )
    public_attachment = json.loads(store.view_bytes("agent-a", "exp_asset_source", "attachments"))[0]
    handle = store.attachment_handle("agent-a", "exp_asset_source", "file_123")
    assert public_attachment["available"] is True
    assert "source_url" not in public_attachment
    assert handle.source_url == source_url
    assert handle.conversation_url == "https://chatgpt.com/c/conversation"


def test_content_references_remain_metadata_not_downloadable_attachments() -> None:
    result = make_export(
        export_id="exp_content_reference",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=ExportStore(ttl_seconds=60),
        browser_payload={
            "messages": [{
                "id": "m",
                "role": "assistant",
                "text": "citation",
                "deep_research": [{"library_file_id": "library-ref-1"}],
                "citations": [{"metadata": {"extra": {"library_file_id": "library-ref-1"}}}],
            }],
            "attachments": [],
        },
        create_download_handles=False,
    )

    assert result["attachments"] == []
    assert result["manifest"]["messages"][0]["deep_research"] == [{"library_file_id": "library-ref-1"}]


def test_library_file_id_without_card_evidence_remains_unavailable() -> None:
    result = make_export(
        export_id="exp_library_file",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=ExportStore(ttl_seconds=60),
        browser_payload={
            "messages": [{"id": "m", "role": "user", "text": "image"}],
            "attachments": [{
                "attachment_id": "libfile-1",
                "file_id": "file-original-1",
                "library_file_id": "libfile-1",
                "filename": "image.png",
                "mime_type": "image/png",
                "size": 512134,
                "message_id": "m",
            }],
        },
    )

    attachment = result["attachments"][0]
    assert attachment["file_kind"] == "library_file"
    assert attachment["file_id"] == "file-original-1"
    assert attachment["size"] == 512134
    assert attachment["source_url"] is None
    assert attachment["download_candidate"] is False
    assert attachment["download_url"] is None
    assert attachment["unavailable_reason"] == "ChatGPT library reference has no verified download source"


def test_library_reference_does_not_merge_with_pointer_using_shared_attachment_id() -> None:
    result = make_export(
        export_id="exp_library_pointer_alias",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=ExportStore(ttl_seconds=60),
        browser_payload={
            "messages": [{
                "id": "m",
                "role": "user",
                "rich_parts": [{
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "sediment://file_pointer#thumbnail",
                }],
            }],
            "attachments": [{
                "attachment_id": "library-id",
                "id": "file_pointer",
                "file_id": "file_pointer",
                "library_file_id": "library-id",
                "filename": "input.png",
                "mime_type": "image/png",
                "size": 101,
                "message_id": "m",
            }],
        },
    )

    assert len(result["attachments"]) == 2
    by_kind = {item["file_kind"]: item for item in result["attachments"]}
    assert by_kind["library_file"]["attachment_id"] == "library-id"
    assert by_kind["library_file"]["downloadable"] is False
    assert by_kind["image_asset_pointer"]["attachment_id"] == "file_pointer"


def test_api_attachment_reference_without_source_is_classified_and_unavailable() -> None:
    result = make_export(
        export_id="exp_api_attachment_ref",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=ExportStore(ttl_seconds=60),
        browser_payload={
            "messages": [{"id": "m", "role": "user", "text": "file"}],
            "attachments": [{
                "attachment_id": "attachment-ref-1",
                "message_id": "m",
            }],
        },
    )

    attachment = result["attachments"][0]
    assert attachment["file_kind"] == "api_attachment"
    assert attachment["downloadable"] is False
    assert attachment["download_candidate"] is False
    assert attachment["source_url"] is None
    assert attachment["unavailable_reason"] == "ChatGPT API attachment reference has no verified download source"


def test_explicit_downloadable_file_id_gets_chatgpt_download_endpoint() -> None:
    store = ExportStore(ttl_seconds=60)
    result = make_export(
        export_id="exp_file",
        conversation_url="https://chatgpt.com/c/conversation",
        scope="all",
        count=10,
        handles=store,
        browser_payload={
            "messages": [{"id": "m", "role": "user", "text": "file"}],
            "attachments": [
                {
                    "file_id": "file_123",
                    "filename": "notes.txt",
                    "message_id": "m",
                    "downloadable": True,
                }
            ],
        },
    )
    attachment = result["attachments"][0]
    assert "/backend-api/files/download/file_123" in attachment["source_url"]
    assert attachment["download_url"].startswith("/exports/download/dl_")


def test_download_handle_is_one_time_and_expires() -> None:
    store = ExportStore(ttl_seconds=60)
    first = store.add_handle("exp", "att", "https://chatgpt.com/files/1")
    assert store.consume(first.handle).attachment_id == "att"
    with pytest.raises(KeyError):
        store.consume(first.handle)
    second = store.add_handle("exp", "att2", "https://chatgpt.com/files/2")
    second.expires_at = 0
    with pytest.raises(KeyError):
        store.consume(second.handle)


class ExportBrowser:
    async def export_conversation(self, conversation_url: str, **_: object) -> dict[str, object]:
        return {
            "messages": [
                {"id": "u", "role": "user", "text": "question"},
                {"id": "a", "role": "assistant", "text": "answer"},
            ]
        }

    async def close(self) -> None:
        return None


def test_manager_updates_only_last_export_summary(tmp_path: Path) -> None:
    async def run() -> None:
        settings = replace(
            Settings.from_env(tmp_path),
            jobs_dir=tmp_path / "jobs",
            browser_profile_dir=tmp_path / "profile",
        )
        manager = JobManager(settings, ExportBrowser())
        job = Job.create("consult", "existing task")
        job.state = "COMPLETE"
        job.conversation_url = "https://chatgpt.com/c/test"
        manager.store.save(job)
        result = await manager.export(task_id=job.id, scope="all")
        current = manager.get(job.id)
        assert current.export_id == result["export_id"]
        assert current.export_scope == "all"
        assert current.export_message_count == 2
        assert current.export_attachment_count == 0
        assert manager.store.read_result(current) is None
        await manager.stop()

    asyncio.run(run())
