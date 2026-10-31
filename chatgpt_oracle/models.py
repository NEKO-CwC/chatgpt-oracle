from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Mode = Literal["consult", "research", "critic"]
Quality = Literal["auto", "extra_high", "pro", "low", "medium", "high"]
State = Literal[
    "QUEUED",
    "STARTING",
    "UPLOADING",
    "ATTACHMENTS_VERIFIED",
    "SEND_PENDING",
    "RUNNING",
    "CANCEL_REQUESTED",
    "COMPLETE",
    "NEEDS_USER_INPUT",
    "TIMEOUT",
    "ERROR",
    "MODEL_UNAVAILABLE",
    "CANCELLED",
]

TERMINAL_STATES = {"COMPLETE", "TIMEOUT", "ERROR", "MODEL_UNAVAILABLE", "CANCELLED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_title(value: str) -> str:
    cleaned = re.sub(r"[^\w\-\u4e00-\u9fff ]+", "", value, flags=re.UNICODE).strip()
    return re.sub(r"\s+", "-", cleaned)[:80] or "chatgpt-task"


@dataclass
class Job:
    id: str
    mode: Mode
    prompt: str
    request_id: str | None = None
    request_intent_sha256: str | None = None
    project: str | None = None
    model: str | None = None
    quality: Quality = "auto"
    reasoning_effort: str | None = None
    allow_downgrade: bool = False
    resolved_model: str | None = None
    resolved_reasoning_effort: str | None = None
    selection_source: str | None = None
    selection_verified_at: str | None = None
    parent_id: str | None = None
    session_id: str | None = None
    upload_ids: list[str] = field(default_factory=list)
    file_refs: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    conversation_url: str | None = None
    state: State = "QUEUED"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    last_checked_at: str | None = None
    progress: str | None = None
    user_action: str | None = None
    error: str | None = None
    error_code: str | None = None
    result_path: str | None = None
    sources: list[str] = field(default_factory=list)
    assistant_count_before: int = 0
    user_message_ids_before: list[str] = field(default_factory=list)
    assistant_message_ids_before: list[str] = field(default_factory=list)
    submitted_prompt_sha256: str | None = None
    current_user_message_id: str | None = None
    current_assistant_message_id: str | None = None
    submission_state: str = "legacy_unverified"
    submission_verified_at: str | None = None
    research_expected: bool = False
    research_start_state: str | None = None
    research_user_message_id: str | None = None
    research_user_prompt_sha256: str | None = None
    research_assistant_message_id: str | None = None
    research_observed: bool = False
    research_state: str | None = None
    research_run_id: str | None = None
    research_report_id: str | None = None
    research_report_complete: bool = False
    # Freeze before any browser mutation. Caller input remains the request
    # identity; this body is the one click's actual Web text.
    sending_text: str | None = None
    sending_text_sha256: str | None = None
    sending_text_normalized_sha256: str | None = None
    oracle_composition_version: str | None = None
    oracle_prepared_at: str | None = None
    oracle_bootstrap_version: str | None = None
    oracle_bootstrap_included: bool = False
    oracle_mode_overlay_included: bool = False
    oracle_recovery_reminder_included: bool = False
    oracle_bootstrap_task_id: str | None = None
    oracle_bootstrap_user_message_id: str | None = None
    oracle_bootstrap_verified_at: str | None = None
    oracle_recovery_pending: bool = False
    clarification_text: str | None = None
    user_input_kind: str | None = None
    cancel_requested_at: str | None = None
    cancellation_stop_verified: bool | None = None
    attempts: int = 0
    # Reserve each runtime browser action durably before it occurs. Restarting
    # a broker must not reset this bounded same-turn recovery budget.
    runtime_recovery_attempts: int = 0
    runtime_recovery_actions: list[dict[str, Any]] = field(default_factory=list)
    # Bounded, secret-scrubbed page evidence (console errors, failed backend
    # requests, visible runtime alerts) kept after the tab is released.
    page_probe_events: list[dict[str, Any]] = field(default_factory=list)
    account_index: int | None = None
    account_attempts: list[dict[str, Any]] = field(default_factory=list)
    account_switch: dict[str, Any] | None = None
    # Metadata for the last explicit conversation export.  Export payloads are
    # intentionally not persisted in the task JSON; only this small summary is.
    export_id: str | None = None
    exported_at: str | None = None
    export_scope: str | None = None
    export_message_count: int = 0
    export_attachment_count: int = 0

    @classmethod
    def create(
        cls,
        mode: Mode,
        prompt: str,
        *,
        project: str | None = None,
        model: str | None = None,
        quality: Quality = "auto",
        reasoning_effort: str | None = None,
        allow_downgrade: bool = False,
        parent_id: str | None = None,
        session_id: str | None = None,
        upload_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> "Job":
        return cls(
            id=f"cgo_{uuid.uuid4().hex[:20]}",
            mode=mode,
            prompt=prompt,
            project=project,
            model=model,
            quality=quality,
            reasoning_effort=reasoning_effort,
            allow_downgrade=allow_downgrade,
            parent_id=parent_id,
            session_id=session_id,
            upload_ids=list(upload_ids or []),
            file_refs=list(file_refs or []),
            attachments=list(attachments or []),
        )

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["prompt_sha256"] = self.prompt_sha256
        data["prompt"] = self.prompt
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})


def public_job(job: Job) -> dict[str, Any]:
    from .oracle_protocol import public_protocol

    return {
        "task_id": job.id,
        "request_id": job.request_id,
        "mode": job.mode,
        "state": job.state,
        "project": job.project,
        "model": job.model,
        "quality": job.quality,
        "reasoning_effort": job.reasoning_effort,
        "allow_downgrade": job.allow_downgrade,
        "resolved_model": job.resolved_model,
        "resolved_reasoning_effort": job.resolved_reasoning_effort,
        "selection_source": job.selection_source,
        "selection_verified_at": job.selection_verified_at,
        "conversation_url": job.conversation_url,
        "current_user_message_id": job.current_user_message_id,
        "current_assistant_message_id": job.current_assistant_message_id,
        "submission_state": job.submission_state,
        "submission_verified_at": job.submission_verified_at,
        "research": {
            "expected": job.research_expected,
            "observed": job.research_observed,
            "state": job.research_state,
            "start_state": job.research_start_state,
            "run_id": job.research_run_id,
            "report_id": job.research_report_id,
            "report_complete": job.research_report_complete,
        },
        "cancel_requested_at": job.cancel_requested_at,
        "cancellation_stop_verified": job.cancellation_stop_verified,
        "session_id": job.session_id,
        "attachments": job.attachments,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "last_checked_at": job.last_checked_at,
        "progress": job.progress,
        "user_action": job.user_action,
        "user_input_kind": job.user_input_kind,
        "clarification_text": job.clarification_text,
        "oracle_protocol": public_protocol(job),
        "error": job.error,
        "error_code": job.error_code,
        "sources": job.sources,
        "prompt_sha256": job.prompt_sha256,
        "attempts": job.attempts,
        "runtime_recovery_attempts": job.runtime_recovery_attempts,
        "runtime_recovery_actions": job.runtime_recovery_actions,
        "page_probe_events": job.page_probe_events,
        "account_index": job.account_index,
        "account_attempts": job.account_attempts,
        "account_switch": job.account_switch,
        "export_id": job.export_id,
        "exported_at": job.exported_at,
        "export_scope": job.export_scope,
        "export_message_count": job.export_message_count,
        "export_attachment_count": job.export_attachment_count,
        "result_available": bool(job.result_path),
        "terminal": job.state in TERMINAL_STATES,
    }
