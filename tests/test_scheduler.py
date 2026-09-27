from __future__ import annotations

import os
from pathlib import Path

import pytest

from chatgpt_oracle.scheduler import (
    IdempotencyConflict,
    Scheduler,
    SchedulerBusy,
)


def test_busy_reject_and_fifo_queue(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    first = scheduler.enqueue("one", "consult", on_busy="reject", retry_after_seconds=30)
    scheduler.set_state("one", "QUEUED")
    assert first["accepted"] is True
    assert scheduler.reserve_next(os.getpid(), "worker-1") == "one"
    assert scheduler.reserve_next(os.getpid(), "worker-2") is None

    with pytest.raises(SchedulerBusy) as exc_info:
        scheduler.enqueue("two", "research", on_busy="reject", retry_after_seconds=180)
    assert exc_info.value.info.active_task_id == "one"

    queued = scheduler.enqueue("two", "research", on_busy="queue", retry_after_seconds=180)
    scheduler.set_state("two", "QUEUED")
    queued_again = scheduler.enqueue(
        "two-retry", "research", on_busy="queue", retry_after_seconds=180, request_id="req-2"
    )
    scheduler.set_state("two-retry", "QUEUED")
    assert queued["queue_position"] == 1
    assert queued_again["queue_position"] == 2
    scheduler.complete("one", "COMPLETE")
    assert scheduler.reserve_next(os.getpid(), "worker-1") == "two"


def test_request_id_is_idempotent(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    first = scheduler.enqueue("one", "consult", on_busy="reject", retry_after_seconds=30, request_id="same")
    retry = scheduler.enqueue("different", "consult", on_busy="reject", retry_after_seconds=30, request_id="same")
    assert first["task_id"] == retry["task_id"] == "one"
    assert retry["idempotent"] is True


def test_request_id_detects_changed_task_inputs(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    scheduler.enqueue(
        "one", "consult", on_busy="reject", retry_after_seconds=30, request_id="same", request_hash="digest-a"
    )
    with pytest.raises(IdempotencyConflict):
        scheduler.enqueue(
            "two", "consult", on_busy="reject", retry_after_seconds=30, request_id="same", request_hash="digest-b"
        )


def test_attachment_materialization_shares_browser_slot_with_jobs(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queued = scheduler.enqueue("task-1", "consult", on_busy="queue", retry_after_seconds=30)
    scheduler.set_state("task-1", "QUEUED")
    assert queued["accepted"] is True
    assert scheduler.acquire_external_slot("tr-download", os.getpid(), "file-owner") is False
    assert scheduler.reserve_next(os.getpid(), "job-owner") == "task-1"
    assert scheduler.acquire_external_slot("tr-download", os.getpid(), "file-owner") is False

    scheduler.complete("task-1", "COMPLETE")
    assert scheduler.acquire_external_slot("tr-download", os.getpid(), "file-owner") is True
    assert scheduler.status()["active_task_id"] == "tr-download"
    assert scheduler.reserve_next(os.getpid(), "job-owner") is None
    scheduler.heartbeat("tr-download", "file-owner")
    scheduler.release_external_slot("tr-download", "file-owner")
    assert scheduler.status()["active_task_id"] is None


def test_export_request_is_durable_idempotent_and_input_bound(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    values = {
        "session_id": "agent-a",
        "conversation_url": "https://chatgpt.com/c/a",
        "scope": "all",
        "count": 10,
        "request_key": "request-key-a",
        "input_fingerprint": "fingerprint-a",
        "on_busy": "queue",
        "retry_after_seconds": 180,
    }
    first = scheduler.enqueue_export("exp-one", **values)
    retry = scheduler.enqueue_export("exp-two", **values)

    assert first["task_id"] == retry["task_id"] == "exp-one"
    assert retry["idempotent"] is True
    assert scheduler.get_export("exp-one")["conversation_url"] == values["conversation_url"]

    with pytest.raises(IdempotencyConflict):
        scheduler.enqueue_export(
            "exp-three",
            **{**values, "input_fingerprint": "different"},
        )


def test_only_export_owner_can_claim_export_queue_item(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    scheduler.enqueue_export(
        "exp-one",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/a",
        scope="all",
        count=10,
        request_key=None,
        input_fingerprint="fingerprint-a",
        on_busy="queue",
        retry_after_seconds=180,
    )
    assert scheduler.reserve_next(os.getpid(), "stdio-worker", allow_exports=False) is None
    assert scheduler.reserve_next(os.getpid(), "http-owner", allow_exports=True) == "exp-one"
    assert scheduler.reserve_next(os.getpid(), "stdio-worker", allow_exports=False) is None


def test_export_audit_persists_only_allowlisted_metadata(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    scheduler.enqueue_export(
        "exp-audit",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/a",
        scope="all",
        count=10,
        request_key=None,
        input_fingerprint="fingerprint-a",
        on_busy="queue",
        retry_after_seconds=180,
    )
    scheduler.record_export_event(
        "exp-audit",
        "READY",
        {
            "source": "dom",
            "message_count": 6,
            "text": "conversation-body-marker",
            "prompt": "prompt-marker",
            "cursor": "cursor-marker",
            "authorization": "Bearer credential-marker",
            "signed_url": "https://files.example/download?signature=signed-marker",
        },
    )
    events = scheduler.export_events("exp-audit")
    assert events[-1]["event"] == "READY"
    assert events[-1]["metadata"] == {"source": "dom", "message_count": 6}
    assert "conversation-body-marker" not in repr(events)
    assert "credential-marker" not in repr(events)
    assert "signed-marker" not in repr(events)

    reloaded = Scheduler(tmp_path)
    assert reloaded.get_export("exp-audit")["state"] == "QUEUED"
    assert reloaded.export_events("exp-audit")[-1]["metadata"] == {
        "source": "dom",
        "message_count": 6,
    }

    scheduler.record_export_event(
        "exp-audit",
        "SOURCE_ATTEMPTS",
        {
            "api_statuses": [
                {
                    "route": "file_download_resource_refresh",
                    "status": 200,
                    "query_keys": ["download_intent", "inline"],
                    "metadata_status": "success",
                    "download_url_present": True,
                    "file_id": "private-id-marker",
                    "source_url": "https://chatgpt.com/private-url-marker",
                }
            ],
            "source_summary": {
                "api_page_count": 2,
                "api_raw_message_count": 65,
                "interpreter_metadata_count": 4,
                "interpreter_metadata_quiet": True,
                "api_attachment_reference_summary": {
                    "attachment_reference_count": 3,
                    "attachment_file_id_count": 0,
                    "attachment_sandbox_path_count": 0,
                    "library_file_id_count": 3,
                    "library_file_id_with_file_id_count": 0,
                    "library_file_id_matching_file_id_count": 0,
                    "library_file_id_pointer_overlap_count": 0,
                    "file_id_pointer_overlap_count": 0,
                    "pointer_reference_count": 6,
                    "library_file_id_with_sandbox_path_count": 0,
                    "rich_part_count": 6,
                    "rich_part_file_id_count": 0,
                    "rich_part_sandbox_path_count": 0,
                    "rich_part_content_type_counts": {"image_asset_pointer": 6},
                    "library_file_id": "private-field-value-marker",
                },
                "attachment_scan": {
                    "observed_message_id_count": 11,
                    "expected_api_message_count": 63,
                    "rendered_count_reaches_api_count": False,
                    "stable": True,
                    "reached_end": True,
                    "stop_reason": "reached_end",
                    "message_ids": ["private-id-marker"],
                },
                "pagination": {
                    "page_count": 2,
                    "raw_message_count": 65,
                    "duplicate_count": 0,
                    "cursor_mismatch_count": 0,
                    "completeness": "verified",
                    "page_flags": [
                        {"has_previous_page": False, "has_next_page": True},
                        {"has_previous_page": True, "has_next_page": False},
                    ],
                    "cursor": "private-cursor-marker",
                },
                "authorization": "private-token-marker",
            },
        },
    )
    source_event = reloaded.export_events("exp-audit")[-1]
    assert source_event["metadata"] == {
        "api_statuses": [
            {
                "route": "file_download_resource_refresh",
                "status": 200,
                "metadata_status": "success",
                "query_keys": ["download_intent", "inline"],
                "download_url_present": True,
            }
        ],
        "source_summary": {
            "api_page_count": 2,
            "api_raw_message_count": 65,
            "interpreter_metadata_count": 4,
            "interpreter_metadata_quiet": True,
            "api_attachment_reference_summary": {
                "attachment_reference_count": 3,
                "attachment_file_id_count": 0,
                "attachment_sandbox_path_count": 0,
                "library_file_id_count": 3,
                "library_file_id_with_file_id_count": 0,
                "library_file_id_matching_file_id_count": 0,
                "library_file_id_pointer_overlap_count": 0,
                "file_id_pointer_overlap_count": 0,
                "pointer_reference_count": 6,
                "library_file_id_with_sandbox_path_count": 0,
                "rich_part_count": 6,
                "rich_part_file_id_count": 0,
                "rich_part_sandbox_path_count": 0,
                "rich_part_content_type_counts": {"image_asset_pointer": 6},
            },
            "attachment_scan": {
                "observed_message_id_count": 11,
                "expected_api_message_count": 63,
                "stable": True,
                "reached_end": True,
                "rendered_count_reaches_api_count": False,
                "stop_reason": "reached_end",
            },
            "pagination": {
                "page_count": 2,
                "raw_message_count": 65,
                "duplicate_count": 0,
                "cursor_mismatch_count": 0,
                "completeness": "verified",
                "page_boundaries": [
                    {"has_previous_page": False, "has_next_page": True},
                    {"has_previous_page": True, "has_next_page": False},
                ],
            },
        },
    }
    persisted = Scheduler(tmp_path).export_events("exp-audit")[-1]
    assert persisted == source_event
    assert "private-id-marker" not in repr(persisted)
    assert "private-url-marker" not in repr(persisted)
    assert "private-cursor-marker" not in repr(persisted)
    assert "private-token-marker" not in repr(persisted)
    assert "private-field-value-marker" not in repr(persisted)


def test_running_export_is_requeued_after_dead_owner_restart(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    scheduler.enqueue_export(
        "exp-recover",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/a",
        scope="all",
        count=10,
        request_key=None,
        input_fingerprint="fingerprint-a",
        on_busy="queue",
        retry_after_seconds=180,
    )
    assert scheduler.reserve_next(999999, "dead-http-owner", allow_exports=True) == "exp-recover"
    scheduler.set_state("exp-recover", "RUNNING", owner_pid=999999)

    scheduler.recover_startup(object())

    assert scheduler.get_export("exp-recover")["state"] == "QUEUED"
    assert scheduler.status("exp-recover")["scheduler_state"] == "QUEUED"
    assert scheduler.export_events("exp-recover")[-1]["event"] == "REQUEUED_AFTER_RESTART"


def test_waiting_queue_has_a_hard_capacity_limit(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for index in range(8):
        task_id = f"queued-{index}"
        scheduler.enqueue(task_id, "consult", on_busy="queue", retry_after_seconds=30)
        scheduler.set_state(task_id, "QUEUED")

    with pytest.raises(SchedulerBusy) as exc_info:
        scheduler.enqueue("overflow", "consult", on_busy="queue", retry_after_seconds=30)

    assert exc_info.value.info.reason == "CHATGPT_QUEUE_FULL"
    assert exc_info.value.info.queue_length == 8


def test_export_admission_obeys_shared_queue_capacity(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for index in range(8):
        scheduler.enqueue_export(
            f"exp-queued-{index}",
            session_id="agent-a",
            conversation_url=f"https://chatgpt.com/c/{index}",
            scope="all",
            count=10,
            request_key=None,
            input_fingerprint=f"fingerprint-{index}",
            on_busy="queue",
            retry_after_seconds=180,
        )

    with pytest.raises(SchedulerBusy) as exc_info:
        scheduler.enqueue_export(
            "exp-overflow",
            session_id="agent-a",
            conversation_url="https://chatgpt.com/c/overflow",
            scope="all",
            count=10,
            request_key=None,
            input_fingerprint="fingerprint-overflow",
            on_busy="queue",
            retry_after_seconds=180,
        )

    assert exc_info.value.info.reason == "CHATGPT_QUEUE_FULL"


def test_export_page_request_count_is_durable_past_previous_limit(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    scheduler.enqueue_export(
        "exp-pages",
        session_id="agent-a",
        conversation_url="https://chatgpt.com/c/pages",
        scope="all",
        count=10,
        request_key=None,
        input_fingerprint="fingerprint-pages",
        on_busy="queue",
        retry_after_seconds=180,
    )
    scheduler.update_export("exp-pages", state="READY")
    with scheduler._connect() as conn:
        conn.execute("UPDATE export_jobs SET page_requests=4096 WHERE export_id=?", ("exp-pages",))

    assert scheduler.record_export_page_request("exp-pages") == 4097

    reloaded = Scheduler(tmp_path)
    assert reloaded.get_export("exp-pages")["page_requests"] == 4097


def test_export_history_pruning_keeps_active_requests(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    scheduler = Scheduler(tmp_path, export_audit_retention_seconds=100)
    for export_id in ("exp-old", "exp-active"):
        scheduler.enqueue_export(
            export_id,
            session_id="agent-a",
            conversation_url=f"https://chatgpt.com/c/{export_id}",
            scope="all",
            count=10,
            request_key=None,
            input_fingerprint=export_id,
            on_busy="queue",
            retry_after_seconds=180,
        )
        scheduler.record_export_event(export_id, "CREATED", {"operation": "test"})
    scheduler.update_export("exp-old", state="FAILED")
    scheduler.complete("exp-old", "ERROR")

    with scheduler._connect() as conn:
        conn.execute("UPDATE export_jobs SET created_at=? WHERE export_id IN (?,?)", (now - 101, "exp-old", "exp-active"))
        conn.execute("UPDATE export_events SET created_at=?", (now - 101,))

    counts = scheduler.prune_export_history(now=now)

    assert counts == {"jobs": 1, "events": 2, "queue_rows": 1}
    assert scheduler.get_export("exp-old") is None
    assert scheduler.get_export("exp-active")["state"] == "QUEUED"
    assert scheduler.status("exp-old")["scheduler_state"] is None
