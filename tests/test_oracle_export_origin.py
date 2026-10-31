import hashlib
import json
import asyncio

import pytest

from chatgpt_oracle.exports import ExportStore, make_export
from chatgpt_oracle.oracle_protocol import ORIGIN_MARKER, normalized_text_sha256


def test_export_origin_requires_receipt_id_and_text_and_does_not_rewrite_snapshot(tmp_path):
    text = ORIGIN_MARKER + "\n\nverified service question"
    messages = [
        {"id": "known", "role": "user", "text": text},
        {"id": "copied", "role": "user", "text": text},
        {"id": "unmarked", "role": "user", "text": "direct or unknown human content"},
        {"id": "bad-text", "role": "user", "text": text + " modified"},
        {"id": "reply", "role": "assistant", "text": "answer"},
    ]
    receipts = {id: {"submitted_prompt_sha256": normalized_text_sha256(text), "task_id": "cgo_original", "request_id": "request-original"}
                for id in ("known", "bad-text")}
    store = ExportStore(snapshot_dir=tmp_path)
    legacy = make_export(export_id="old", conversation_url="https://chatgpt.com/c/test", scope="all", count=10,
                         browser_payload={"messages": messages}, handles=store)
    old_status = store.put_snapshot("old", session_id="reader", conversation_url="https://chatgpt.com/c/test", payload=legacy,
                                   source="fixture", completeness={"messages": "verified", "attachments": "verified", "overall": "verified"})
    old_hash = old_status["views"]["messages"]["sha256"]
    enriched = make_export(export_id="new", conversation_url="https://chatgpt.com/c/test", scope="all", count=10,
                           browser_payload={"messages": messages}, handles=store, origin_receipts=receipts)
    exported = enriched["manifest"]["messages"]
    assert exported[0]["origin"]["source"] == "service_codex"
    assert exported[0]["origin"]["request_id"] == "request-original"
    assert all(m["origin"]["source"] == "unknown" for m in exported[1:])
    assert exported[1]["origin"]["marker_present"] is True
    assert exported[2]["origin"]["marker_present"] is False
    assert "origin" not in messages[0] and "origin" not in legacy["manifest"]["messages"][0]
    assert store.snapshot_status("reader", "old")["views"]["messages"]["sha256"] == old_hash
    assert enriched["markdown"] == legacy["markdown"]


def test_complete_unique_text_bridges_a_confirmed_dom_placeholder_to_api_uuid(tmp_path):
    text = ORIGIN_MARKER + "\n\nunique service question"
    receipt = {"task_id": "cgo_proven", "user_message_id": "fallback-turn-0:0", "submitted_prompt_sha256": normalized_text_sha256(text)}
    def export(messages, *, complete=False, receipts=None):
        return make_export(export_id="new", conversation_url="https://chatgpt.com/c/test", scope="all", count=10,
                           browser_payload={"messages": messages}, handles=ExportStore(),
                           origin_receipts=receipts or {"fallback-turn-0:0": receipt}, allow_unique_text_receipts=complete)["manifest"]["messages"]
    messages = [{"id": "api-user-uuid", "role": "user", "text": text}]
    assert export(messages)[0]["origin"]["source"] == "unknown"
    proven = export(messages, complete=True)[0]["origin"]
    assert proven["source"] == "service_codex" and proven["evidence"] == "confirmed_service_turn_unique_text"
    assert proven["user_message_id"] == "fallback-turn-0:0" and proven["export_message_id"] == "api-user-uuid"
    duplicate = messages + [{"id": "human-copy", "role": "user", "text": text}]
    assert all(m["origin"]["source"] == "unknown" for m in export(duplicate, complete=True))
    receipts = {"first-ui": receipt, "second-ui": {**receipt, "task_id": "cgo_second"}}
    assert export(messages, complete=True, receipts=receipts)[0]["origin"]["source"] == "unknown"


@pytest.mark.parametrize("complete", [True, False])
def test_manager_gates_unique_origin_on_a_complete_api_snapshot(tmp_path, complete):
    from test_oracle_protocol import setup
    from test_jobs import wait_terminal
    async def run():
        manager, browser = setup(tmp_path)
        try:
            job = await manager.submit("consult", "new unique question", session_id="export-origin")
            job = await wait_terminal(manager, job.id)
            async def export(url, **kwargs):
                return {"source":"api_paged", "messages":[{"id":"canonical-user", "role":"user", "text":job.sending_text}],
                        "completeness":{"messages":"verified" if complete else "partial", "attachments":"verified", "overall":"verified" if complete else "partial"}}
            browser.export_conversation = export
            snapshot = await manager.export(session_id="export-origin", scope="all", request_id="export-request")
            origin = snapshot["manifest"]["messages"][0]["origin"]
            assert origin["source"] == ("service_codex" if complete else "unknown")
            assert origin["evidence"] == ("confirmed_service_turn_unique_text" if complete else None)
        finally:
            await manager.stop()
    asyncio.run(run())
