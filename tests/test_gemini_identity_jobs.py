"""Gemini same-turn recovery survives broker restart without another send.

These tests use temporary stores and a fake adapter; they never connect to CDP.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.gemini_browser import GeminiBrowser, GeminiTaskError, identify_messages, matched_turn
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job, utc_now
from chatgpt_oracle.oracle_protocol import normalized_text_sha256


URL = "https://gemini.google.com/app/original-identity-turn"
RELOAD = "reload_gemini_conversation"


def settings(tmp_path):
    return replace(
        Settings.from_env(tmp_path, provider="gemini"),
        jobs_dir=tmp_path / "jobs", sessions_dir=tmp_path / "sessions",
        file_store_dir=tmp_path / "files", browser_profile_dir=tmp_path / "profile",
        shared_browser=False, browser_timeout_seconds=1,
        consult_poll_seconds=.005, consult_timeout_seconds=3,
    )


def missing_turn():
    return BrowserSnapshot(
        "RECOVERABLE_ERROR", URL, runtime_error_code="GEMINI_TURN_IDENTITY_MISSING",
        result_text="Uncorrelated content must never become the answer.",
        generation_active=False,
    )


def complete():
    return BrowserSnapshot(
        "COMPLETE", URL, result_text="The original Gemini answer.",
        generation_active=False,
    )


class RecoveryGemini:
    requires_turn_receipts = True
    continuation_accepts_mode = True

    def __init__(self, snapshots, recover=None):
        self.snapshots = list(snapshots)
        self.recover = recover
        self.manager = None
        self.job_id = None
        self.inspect_receipts = []
        self.recovery_calls = []
        self.actions = []
        self.send_calls = []

    async def submit(self, *args, **kwargs):
        self.send_calls.append("submit")
        raise AssertionError("A recovered Gemini turn must never be submitted again")

    async def continue_conversation(self, *args, **kwargs):
        self.send_calls.append("continue_conversation")
        raise AssertionError("Recovery must never create a Gemini continuation")

    async def inspect(self, receipt):
        self.inspect_receipts.append(deepcopy(receipt))
        snapshot = self.snapshots.pop(0) if self.snapshots else complete()
        if snapshot.state == "COMPLETE":
            receipt.current_assistant_message_id = "original-answer"
        return snapshot

    async def recover_runtime_error(self, receipt, *, attempt, on_recovery_prepared):
        self.recovery_calls.append(attempt)
        if self.recover:
            return await self.recover(receipt, attempt, on_recovery_prepared)
        await on_recovery_prepared(RELOAD)
        self.assert_durable_reservation(receipt, attempt)
        self.actions.append(RELOAD)
        return True

    def assert_durable_reservation(self, receipt, attempt):
        saved = self.manager.store.get(self.job_id)
        assert saved.runtime_recovery_attempts == receipt.runtime_recovery_attempts == attempt
        assert saved.runtime_recovery_actions[-1]["attempt"] == attempt
        assert saved.runtime_recovery_actions[-1]["action"] == RELOAD
        assert saved.runtime_recovery_actions[-1]["prepared_at"]
        assert saved.state == "RUNNING"

    async def close(self):
        pass


def manager_for(tmp_path, browser):
    manager = JobManager(settings(tmp_path), browser)
    browser.manager = manager
    assert manager.policy.name == "gemini"
    return manager


def seed(manager, browser, *, attempts=0):
    job = Job.create(
        "consult", "Original caller's correction discussion.",
        session_id="gemini-original-session", parent_id="cgo_original_parent",
        model="Argon", quality="high", reasoning_effort="high", allow_downgrade=False,
    )
    job.request_id = "original-gemini-correction-request"
    job.conversation_url = URL
    job.state = "RUNNING"
    job.sending_text = "【CODEX_ORACLE】\nThe original frozen correction question."
    job.submitted_prompt_sha256 = normalized_text_sha256(job.sending_text)
    job.assistant_count_before = 3
    job.user_message_ids_before = ["original-user-before"]
    job.assistant_message_ids_before = ["original-answer-before"]
    job.current_user_message_id = "original-user"
    job.submission_state = "confirmed"
    job.submission_verified_at = utc_now()
    job.resolved_model = "4 Argon"
    job.resolved_reasoning_effort = "High"
    job.selection_source = "visible_picker"
    job.selection_verified_at = utc_now()
    job.runtime_recovery_attempts = attempts
    job.runtime_recovery_actions = [
        {"attempt": i, "action": RELOAD, "prepared_at": utc_now(),
         "runtime_error_code": "GEMINI_TURN_IDENTITY_MISSING"}
        for i in range(1, attempts + 1)
    ]
    manager.store.save(job)
    manager.sessions.touch(job.session_id, conversation_url=URL, last_task_id=job.id)
    browser.job_id = job.id
    return job


def original_identity(job):
    return {key: deepcopy(getattr(job, key)) for key in (
        "id", "request_id", "parent_id", "session_id", "prompt", "sending_text",
        "conversation_url", "model", "quality", "reasoning_effort", "allow_downgrade",
        "resolved_model", "resolved_reasoning_effort", "selection_source",
        "selection_verified_at", "submitted_prompt_sha256", "user_message_ids_before",
        "assistant_message_ids_before", "current_user_message_id", "submission_state",
        "submission_verified_at",
    )}


@pytest.mark.parametrize("consumed", [0, 1])
def test_confirmed_gemini_recovers_and_keeps_original_identity(tmp_path, consumed):
    async def run():
        browser = RecoveryGemini([missing_turn(), complete()])
        manager = manager_for(tmp_path, browser)
        job = seed(manager, browser, attempts=consumed)
        identity = original_identity(job)
        previous_actions = deepcopy(job.runtime_recovery_actions)
        try:
            await manager._process_body(job)
            saved = manager.get(job.id)
            assert saved.state == "COMPLETE"
            assert original_identity(saved) == identity
            assert browser.send_calls == []
            assert browser.recovery_calls == [consumed + 1]
            assert browser.actions == [RELOAD]
            assert saved.runtime_recovery_attempts == consumed + 1
            assert saved.runtime_recovery_actions[:consumed] == previous_actions
            assert len(saved.runtime_recovery_actions) == consumed + 1
            public = manager.public(job.id)
            assert public["provider"] == "gemini"
            assert public["runtime_recovery_attempts"] == consumed + 1
            assert public["runtime_recovery_actions"] == saved.runtime_recovery_actions
            assert public["terminal"] is True
            result = manager.store.read_result(saved)
            assert "original Gemini answer" in result
            assert "Uncorrelated content" not in result
            assert [r.runtime_recovery_attempts for r in browser.inspect_receipts] == [consumed, consumed + 1]
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("consumed", [0, 1, 2])
def test_gemini_recovery_has_one_durable_two_action_budget(tmp_path, consumed):
    async def run():
        browser = RecoveryGemini([missing_turn(), missing_turn(), missing_turn()])
        manager = manager_for(tmp_path, browser)
        job = seed(manager, browser, attempts=consumed)
        identity = original_identity(job)
        try:
            await manager._process_body(job)
            saved = manager.get(job.id)
            assert saved.state == "ERROR"
            assert saved.error_code == "RUNTIME_RECOVERY_EXHAUSTED"
            assert saved.runtime_recovery_attempts == 2
            assert [a["attempt"] for a in saved.runtime_recovery_actions] == [1, 2]
            assert browser.recovery_calls == list(range(consumed + 1, 3))
            assert len(browser.inspect_receipts) == 3 - consumed
            assert original_identity(saved) == identity
            assert browser.send_calls == []
            assert saved.result_path is None
            assert saved.user_action is saved.user_input_kind is None
            assert manager.public(job.id)["terminal"] is True
        finally:
            await manager.stop()
    asyncio.run(run())


def test_gemini_restart_after_reserved_reload_only_observes_original_turn(tmp_path):
    async def run():
        entered = asyncio.Event()
        first = RecoveryGemini([missing_turn()])
        manager = manager_for(tmp_path, first)
        job = seed(manager, first)
        identity = original_identity(job)

        async def interrupted(receipt, attempt, prepared):
            await prepared(RELOAD)
            first.assert_durable_reservation(receipt, attempt)
            first.actions.append(RELOAD)
            entered.set()
            await asyncio.Event().wait()

        first.recover = interrupted
        manager.scheduler.enqueue(job.id, job.mode, session_id=job.session_id,
                                  on_busy="queue", retry_after_seconds=1)
        manager.scheduler.set_state(job.id, "QUEUED")
        await manager.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            await manager.stop()

        persisted = manager.get(job.id)
        assert persisted.state == "RUNNING"
        assert persisted.runtime_recovery_attempts == 1
        assert original_identity(persisted) == identity
        assert manager.scheduler.status(job.id)["scheduler_state"] == "QUEUED"
        first_action = deepcopy(persisted.runtime_recovery_actions)

        second = RecoveryGemini([missing_turn(), missing_turn()])
        restarted = manager_for(tmp_path, second)
        second.job_id = job.id
        try:
            await restarted.start()
            async with asyncio.timeout(2):
                while not restarted.public(job.id)["terminal"]:
                    await asyncio.sleep(.005)
            saved = restarted.get(job.id)
            assert saved.state == "ERROR"
            assert saved.error_code == "RUNTIME_RECOVERY_EXHAUSTED"
            assert saved.runtime_recovery_attempts == 2
            assert saved.runtime_recovery_actions[:1] == first_action
            assert [a["action"] for a in saved.runtime_recovery_actions] == [RELOAD] * 2
            assert second.recovery_calls == [2]
            assert second.actions == [RELOAD]
            assert [r.runtime_recovery_attempts for r in second.inspect_receipts] == [1, 2]
            assert first.send_calls == second.send_calls == []
            assert original_identity(saved) == identity
            assert restarted.sessions.get(job.session_id).conversation_url == URL
            assert len(restarted.store.all()) == 1
        finally:
            await restarted.stop()
    asyncio.run(run())


def test_gemini_second_reload_reports_missing_turn_as_a_service_error(tmp_path):
    async def run():
        browser = RecoveryGemini([missing_turn()])
        manager = manager_for(tmp_path, browser)
        job = seed(manager, browser, attempts=1)
        identity = original_identity(job)

        async def still_missing(receipt, attempt, prepared):
            assert attempt == 2
            await prepared(RELOAD)
            browser.assert_durable_reservation(receipt, attempt)
            browser.actions.append(RELOAD)
            raise GeminiTaskError("TURN_NOT_VISIBLE", "The original turn is still absent; nothing was resent")

        browser.recover = still_missing
        try:
            await manager._process_body(job)
            saved = manager.get(job.id)
            assert saved.state == "ERROR" and saved.error_code == "TURN_NOT_VISIBLE"
            assert saved.runtime_recovery_attempts == 2
            assert browser.recovery_calls == [2] and len(browser.inspect_receipts) == 1
            assert browser.actions == [RELOAD] and browser.send_calls == []
            assert saved.user_action is saved.user_input_kind is None
            assert saved.result_path is None and original_identity(saved) == identity
            assert manager.public(job.id)["terminal"] is True
        finally:
            await manager.stop()
    asyncio.run(run())


def matcher_receipt(baseline, prompt="The repeated question"):
    return SubmitReceipt(
        URL, sum(m["role"] == "assistant" for m in baseline),
        user_message_ids_before=[m["id"] for m in baseline if m["role"] == "user"],
        assistant_message_ids_before=[m["id"] for m in baseline if m["role"] == "assistant"],
        submitted_prompt_sha256=normalized_text_sha256(prompt),
        current_user_message_id="original-identity-no-longer-rendered", submission_state="confirmed",
    )


def test_rematch_never_takes_a_historical_duplicate_with_a_changed_predecessor():
    baseline = identify_messages([
        {"role": "assistant", "id": "history-before-duplicate", "text": "History."},
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "historical-duplicate-answer", "text": "Wrong old answer."},
        {"role": "user", "text": "The previous round"},
        {"role": "assistant", "id": "baseline-tail", "text": "The previous answer."},
    ])
    receipt = matcher_receipt(baseline)
    # Virtualization drops the duplicate's old predecessor. Its newly derived
    # user ID is absent from the baseline, but it is still a historical turn.
    current = identify_messages([
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "historical-duplicate-answer", "text": "Wrong old answer."},
        {"role": "user", "text": "The previous round"},
        {"role": "assistant", "id": "baseline-tail", "text": "The previous answer."},
    ])
    assert current[0]["id"] not in receipt.user_message_ids_before
    assert matched_turn(current, receipt) == (None, [])


def test_rematch_never_skips_an_intervening_user_to_a_human_repeat():
    baseline = identify_messages([
        {"role": "user", "text": "The previous round"},
        {"role": "assistant", "id": "baseline-tail", "text": "The previous answer."},
    ])
    receipt = matcher_receipt(baseline)
    current = identify_messages([
        *baseline,
        {"role": "user", "text": "Human intervening question"},
        {"role": "assistant", "id": "intervening-answer", "text": "Human answer."},
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "later-human-answer", "text": "Not our answer."},
    ])
    assert matched_turn(current, receipt) == (None, [])


def test_pinned_original_turn_returns_its_answer_before_a_later_human_repeat():
    baseline = identify_messages([
        {"role": "user", "text": "The previous round"},
        {"role": "assistant", "id": "baseline-tail", "text": "The previous answer."},
    ])
    current = identify_messages([
        *baseline,
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "original-answer", "text": "Our original answer."},
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "later-human-answer", "text": "Not our answer."},
    ])
    receipt = matcher_receipt(baseline)
    receipt.current_user_message_id = current[2]["id"]
    user, replies = matched_turn(current, receipt)
    assert user["id"] == current[2]["id"]
    assert [m["id"] for m in replies] == ["original-answer"]


def test_known_answer_cannot_be_replaced_by_a_duplicate_with_a_colliding_user_id():
    baseline = identify_messages([
        {"role": "user", "text": "The previous round"},
        {"role": "assistant", "id": "baseline-tail", "text": "The previous answer."},
    ])
    original = identify_messages([
        *baseline,
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "original-answer", "text": "Our original answer."},
    ])
    receipt = matcher_receipt(baseline)
    receipt.current_user_message_id = original[2]["id"]
    receipt.current_assistant_message_id = "original-answer"
    # Replacing the visible branch or evicting its intervening messages can
    # give a later duplicate the same predecessor/text-derived user identity.
    replacement = identify_messages([
        *baseline,
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "different-answer", "text": "Not our answer."},
    ])
    assert replacement[2]["id"] == receipt.current_user_message_id
    _, replies = matched_turn(replacement, receipt)
    assert replies == [], "An already bound answer must not be rebound to a new assistant identity"


def test_known_answer_wins_over_an_adjacent_unbound_assistant_variant():
    current = identify_messages([
        {"role": "user", "text": "The repeated question"},
        {"role": "assistant", "id": "original-answer", "text": "Our original answer."},
        {"role": "assistant", "id": "alternate-answer", "text": "Unbound regenerated variant."},
    ])
    receipt = matcher_receipt([])
    receipt.current_user_message_id = current[0]["id"]
    receipt.current_assistant_message_id = "original-answer"
    _, replies = matched_turn(current, receipt)
    assert [m["id"] for m in replies] == ["original-answer"]


def test_original_turn_starting_generation_during_reservation_skips_reload(tmp_path):
    async def run():
        browser = GeminiBrowser(settings(tmp_path))
        baseline = identify_messages([
            {"role": "user", "text": "The previous round"},
            {"role": "assistant", "id": "baseline-tail", "text": "The previous answer."},
        ])
        receipt = matcher_receipt(baseline)
        arrived = identify_messages([*baseline, {"role": "user", "text": "The repeated question"}])
        receipt.current_user_message_id = arrived[-1]["id"]
        page = SimpleNamespace(evaluate=AsyncMock(return_value={"image_count": 0, "busy": False}))
        browser._runtime_owned_page = AsyncMock(return_value=(page, "original-target"))
        browser._ready = AsyncMock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value="")))
        browser._surface = AsyncMock(return_value={"messages": arrived, "active": True, "alerts": []})
        surface, may_reload = await browser._runtime_recovery_surface(page, "original-target", receipt)
        assert may_reload is False
        assert surface["active"] is True
        assert receipt.current_user_message_id == arrived[-1]["id"]
    asyncio.run(run())
