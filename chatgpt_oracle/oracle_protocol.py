"""Versioned user-message composition; this is not a Web system prompt."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.resources import files
from typing import Any

ORIGIN_MARKER = "【CODEX_ORACLE】"
PROTOCOL_VERSION = "oracle-v1"
BOOTSTRAP_VERSION = "oracle-bootstrap-v1"
MEMORY_POLICY = "prompt_only"
RECOVERY_REMINDER = "继续本对话已建立的 Oracle 协作约定。"

MODE_OVERLAYS = {
    "consult": "本轮是开放咨询。优先重新理解问题、提出竞争解释并继续有价值的讨论；不要求唯一结论或实施计划。",
    "critic": "本轮是对具体判断或方案的有限对抗性审查。检查隐含假设、反例、失败模式和替代解释；不把原 framing 当成已证明事实，不强制给出实施建议。",
    "research": "本轮是外部研究。先建立当前事实和可核对来源，再区分解释与尚未确认的问题；研究结论不直接证明本地实现或验收完成。",
}


def normalized_text_sha256(text: str) -> str:
    """The browser compares rendered text after harmless whitespace changes."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def bootstrap_text() -> str:
    return files("chatgpt_oracle").joinpath("prompts", f"{BOOTSTRAP_VERSION}.md").read_text(encoding="utf-8").strip()


def compose_message(
    prompt: str, mode: str, *, include_bootstrap: bool,
    include_mode_overlay: bool, recovery_reminder: bool = False,
) -> str:
    # Only redundant standalone leading markers are removed. A quotation or a
    # marker embedded in the caller's body remains untouched.
    body = prompt.strip()
    while body.splitlines() and body.splitlines()[0].strip() == ORIGIN_MARKER:
        body = "\n".join(body.splitlines()[1:]).lstrip()
    if not body:
        raise ValueError("prompt must contain content beyond the origin marker")
    parts = [ORIGIN_MARKER]
    if include_bootstrap:
        parts.append(bootstrap_text())
    elif recovery_reminder:
        parts.append(RECOVERY_REMINDER)
    if include_mode_overlay:
        parts.append(MODE_OVERLAYS[mode])
    parts.append(body)
    return "\n\n".join(parts)


def protocol_capabilities() -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "origin_marker": ORIGIN_MARKER,
        "marker_enforced": True,
        "bootstrap_version": BOOTSTRAP_VERSION,
        "bootstrap_sha256": hashlib.sha256(bootstrap_text().encode("utf-8")).hexdigest(),
        "bootstrap_delivery": "confirmed_user_turn",
        "memory_policy": MEMORY_POLICY,
        "new_chat_kind": "ordinary",
        "draft_policy": "reject_nonempty",
        "account_page_recovery": {"max_refreshes_per_operation": 2, "before_send_only": True, "login_logout_captcha": "human"},
    }


def confirmed_bootstrap(job: Any) -> bool:
    return bool(
        job.oracle_bootstrap_included and job.oracle_bootstrap_version
        and job.sending_text and job.oracle_prepared_at
        and job.submission_state == "confirmed" and job.current_user_message_id
        and job.submission_verified_at
        and job.submitted_prompt_sha256 == job.sending_text_normalized_sha256
        and job.sending_text_normalized_sha256 == normalized_text_sha256(job.sending_text)
    )


def public_protocol(job: Any) -> dict[str, Any]:
    delivered = confirmed_bootstrap(job)
    inherited = bool(job.oracle_bootstrap_task_id and job.oracle_bootstrap_user_message_id)
    return {
        "version": job.oracle_composition_version,
        "origin_marker": ORIGIN_MARKER if job.oracle_composition_version else None,
        "marker_enforced": bool(job.oracle_composition_version and job.sending_text and job.sending_text.startswith(ORIGIN_MARKER + "\n")),
        "memory_policy": MEMORY_POLICY,
        "mode": job.mode,
        "mode_overlay_included": job.oracle_mode_overlay_included,
        "recovery_reminder_included": job.oracle_recovery_reminder_included,
        "prepared_at": job.oracle_prepared_at,
        "sending_text_sha256": job.sending_text_sha256,
        "sending_text_normalized_sha256": job.sending_text_normalized_sha256,
        "bootstrap": {
            "version": job.oracle_bootstrap_version,
            "included_this_turn": job.oracle_bootstrap_included,
            "state": "confirmed" if delivered or inherited else "prepared" if job.oracle_bootstrap_included else "unknown",
            "task_id": job.id if delivered else job.oracle_bootstrap_task_id,
            "user_message_id": job.current_user_message_id if delivered else job.oracle_bootstrap_user_message_id,
            "verified_at": job.submission_verified_at if delivered else job.oracle_bootstrap_verified_at,
        },
    }
