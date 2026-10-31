from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserUnresponsiveError, ChatGPTBrowser, SubmitReceipt, _prompt_text_sha256, _resolve_current_turn
from chatgpt_oracle.config import Settings


URL = "https://chatgpt.com/c/turn-test"


def message(identity: str, role: str, text: str, **extra):
    return {"id": identity, "role": role, "text": text, **extra}


def receipt(*, confirmed=False, **extra):
    fields = {
        "user_message_ids_before": ["user-old"],
        "assistant_message_ids_before": ["assistant-old"],
        "submitted_prompt_sha256": _prompt_text_sha256("current question"),
        "submission_state": "confirmed" if confirmed else "unconfirmed",
    }
    if confirmed:
        fields["current_user_message_id"] = "user-current"
    return SubmitReceipt(URL, 5, **(fields | extra))


def inspect(tmp_path, submission, messages, *, active=False, **surface):
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile"))
        page = SimpleNamespace(url=URL, frames=[])
        browser._conversation_page = AsyncMock(return_value=page)
        browser._read_turn_surface = AsyncMock(return_value={
            "messages": messages, "generation_active": active, "page_title": "turn test", **surface,
        })
        return await browser.inspect(submission)
    return asyncio.run(run())


def test_virtualized_reply_is_identified_when_assistant_count_decreases(tmp_path):
    submission = receipt()
    snapshot = inspect(tmp_path, submission, [
        message("assistant-old", "assistant", "old answer"),
        message("user-current", "user", "current question"),
        message("assistant-current", "assistant", "new answer"),
    ])
    assert snapshot.state == "COMPLETE"
    assert snapshot.result_text == "new answer"
    assert snapshot.current_user_message_id == "user-current"
    assert snapshot.current_assistant_message_id == "assistant-current"
    assert snapshot.submission_state == "confirmed"
    assert snapshot.submission_verified_at


def test_old_titles_and_sources_cannot_become_current_progress_or_result(tmp_path):
    snapshot = inspect(tmp_path, receipt(confirmed=True), [
        message("assistant-old", "assistant", "Research sources: old title", sources=["https://old.example/"]),
        message("user-current", "user", "current question"),
    ], empty_page_text="Research sources: stale title")
    assert snapshot.state == "RUNNING"
    assert snapshot.result_text is None
    assert "old" not in snapshot.progress
    assert "stale" not in snapshot.progress
    assert snapshot.sources == []


def test_unseen_old_assistant_without_matching_user_is_not_a_new_turn(tmp_path):
    snapshot = inspect(tmp_path, receipt(), [
        message("user-old", "user", "old question"),
        message("assistant-old", "assistant", "old answer"),
        message("assistant-newly-rendered-old", "assistant", "old sources title"),
    ])
    assert snapshot.state == "NEEDS_USER_INPUT"
    assert snapshot.submission_state == "unconfirmed"
    assert snapshot.result_text is None
    assert snapshot.progress is None


def test_same_prompt_on_baseline_user_does_not_confirm_a_send():
    submission = receipt()
    assert _resolve_current_turn(submission, [
        message("user-old", "user", "current question"),
        message("assistant-old", "assistant", "old answer"),
    ]) == []
    assert submission.submission_state == "unconfirmed"


def test_newly_rendered_old_matching_user_before_baseline_anchor_is_ambiguous():
    submission = receipt()
    assert _resolve_current_turn(submission, [
        message("older-unseen-user", "user", "current question"),
        message("older-unseen-assistant", "assistant", "older answer"),
        message("assistant-old", "assistant", "last old answer"),
    ]) == []
    assert submission.current_user_message_id is None


def test_matching_prompt_without_any_rendered_baseline_anchor_is_ambiguous():
    submission = receipt()
    assert _resolve_current_turn(submission, [
        message("unseen-user", "user", "current question"),
        message("unseen-assistant", "assistant", "possibly old answer"),
    ]) == []
    assert submission.submission_state == "unconfirmed"


def test_an_earlier_baseline_anchor_cannot_replace_the_latest_prior_assistant():
    submission = receipt(assistant_message_ids_before=["assistant-earlier", "assistant-old"])
    assert _resolve_current_turn(submission, [
        message("assistant-earlier", "assistant", "earlier old answer"),
        message("unseen-old-user", "user", "current question"),
        message("unseen-old-assistant", "assistant", "unseen old answer"),
    ]) == []
    assert submission.current_user_message_id is None


def test_correlated_assistant_survives_user_eviction_without_absorbing_other_turns(tmp_path):
    snapshot = inspect(tmp_path, receipt(confirmed=True, current_assistant_message_id="assistant-current"), [
        message("assistant-old", "assistant", "old answer"),
        message("assistant-current", "assistant", "current answer"),
        message("assistant-unrelated", "assistant", "an unrelated answer whose user was virtualized"),
    ])
    assert snapshot.state == "COMPLETE"
    assert snapshot.result_text == "current answer"


def test_active_generation_prevents_early_completion_and_sources_are_current_only(tmp_path):
    messages = [
        message("assistant-old", "assistant", "old sources", sources=["https://old.example/"]),
        message("user-current", "user", "current question"),
        message("assistant-current", "assistant", "new sources", sources=["https://new.example/"]),
    ]
    snapshot = inspect(tmp_path, receipt(confirmed=True), messages, active=True)
    assert snapshot.state == "RUNNING"
    assert snapshot.generation_active is True
    assert snapshot.result_text is None
    assert snapshot.sources == ["https://new.example/"]
    complete = inspect(tmp_path, receipt(confirmed=True), messages)
    assert complete.state == "COMPLETE"
    assert complete.result_text == "new sources"


def test_thinking_placeholder_and_empty_assistant_are_not_answers(tmp_path):
    for placeholder in ("Pro Thinking…", "", "正在思考..."):
        snapshot = inspect(tmp_path, receipt(confirmed=True), [
            message("user-current", "user", "current question"),
            message("assistant-current", "assistant", placeholder),
        ])
        assert snapshot.state == "RUNNING"
        assert snapshot.result_text is None


def test_following_user_turn_is_a_hard_boundary(tmp_path):
    snapshot = inspect(tmp_path, receipt(confirmed=True), [
        message("user-current", "user", "current question"),
        message("assistant-current", "assistant", "current answer"),
        message("user-other", "user", "a later question"),
        message("assistant-other", "assistant", "a later answer"),
    ])
    assert snapshot.result_text == "current answer"
    assert snapshot.current_assistant_message_id == "assistant-current"


def test_prompt_candidates_preserve_exact_text_with_file_tile_wrappers():
    submission = receipt()
    output = _resolve_current_turn(submission, [
        message("assistant-old", "assistant", "last old answer"),
        message("user-current", "user", "context.zip Zip Archive current question",
                prompt_text_candidates=["context.zip", "Zip Archive", "current\nquestion"]),
        message("assistant-current", "assistant", "read the file"),
    ])
    assert [item["id"] for item in output] == ["assistant-current"]
    assert submission.submission_state == "confirmed"
    ambiguous = receipt()
    assert _resolve_current_turn(ambiguous, [
        message("assistant-old", "assistant", "last old answer"),
        message("user-current", "user", "prefix current question suffix"),
    ]) == []


def test_legacy_count_only_receipt_is_explicitly_unverified(tmp_path):
    snapshot = inspect(tmp_path, SubmitReceipt(URL, 0), [message("assistant-old", "assistant", "old answer")])
    assert snapshot.state == "NEEDS_USER_INPUT"
    assert snapshot.submission_state == "legacy_unverified"
    assert snapshot.result_text is None
    assert "勿重复提交" in snapshot.user_action


def test_bounded_send_confirmation_keeps_one_click_and_extra_high_receipt(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(
            Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile", browser_timeout_seconds=0.025,
        ))
        # This test isolates send acknowledgement; DOM preparation has its own
        # real-editor regressions and Page objects are identity-hashable.
        page = type("PageStub", (), {"url": URL, "bring_to_front": AsyncMock(), "frames": []})()
        browser._conversation_page = AsyncMock(return_value=page)
        browser._require_composer = AsyncMock()
        browser._apply_selection = AsyncMock(return_value={
            "resolved_model": None, "resolved_reasoning_effort": "Extra High",
            "selection_source": "explicit", "selection_verified_at": "verified",
        })
        browser._verify_selection_after_upload = AsyncMock()
        browser._upload_files = AsyncMock(return_value=[])
        browser._find_composer = AsyncMock(return_value=SimpleNamespace(fill=AsyncMock(), evaluate=AsyncMock(return_value={"text": "", "prompt_text": "", "has_mention": False})))
        browser._check_composer_draft = AsyncMock(return_value=browser._find_composer.return_value)
        browser._fill_prepared_draft = AsyncMock()
        browser._read_turn_surface = AsyncMock(return_value={"messages": [
            message("user-old", "user", "old question"),
            message("assistant-old", "assistant", "old answer"),
        ]})
        browser._click_send = AsyncMock()
        browser._wait_for_conversation_url = AsyncMock()
        submission = await browser.continue_conversation(URL, "current question", quality="extra_high", allow_downgrade=False)
        assert browser._click_send.await_count == 1
        browser._verify_selection_after_upload.assert_awaited_once()
        assert submission.submission_state == "unconfirmed"
        assert submission.current_user_message_id is None
        assert submission.resolved_reasoning_effort == "Extra High"
        assert submission.requested_quality == "extra_high"
        assert submission.conversation_url == URL
        browser._apply_selection.assert_awaited_once_with(
            page, quality="extra_high", model=None, reasoning_effort=None, allow_downgrade=False,
        )
    asyncio.run(run())


def test_cancellation_interrupts_send_confirmation(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(
            Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile", browser_timeout_seconds=1,
        ))
        observed = asyncio.Event()
        async def wait(_page):
            observed.set()
            await asyncio.Event().wait()
        browser._read_turn_surface = wait
        submission = receipt()
        pending = asyncio.create_task(browser._confirm_submission(SimpleNamespace(), submission))
        await asyncio.wait_for(observed.wait(), timeout=1)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("send acknowledgement swallowed cancellation")
        assert submission.submission_state == "unconfirmed"
    asyncio.run(run())


def test_send_confirmation_captures_assistant_shell_before_long_user_eviction(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(
            Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile", browser_timeout_seconds=1,
        ))
        page = SimpleNamespace(url=URL, frames=[])
        first = [message("assistant-old", "assistant", "old answer"), message("user-current", "user", "current question")]
        second = [*first, message("assistant-current", "assistant", "Thinking…")]
        browser._read_turn_surface = AsyncMock(side_effect=[{"messages": first}, {"messages": second}])
        submission = receipt()
        await browser._confirm_submission(page, submission)
        assert submission.submission_state == "confirmed"
        assert submission.current_assistant_message_id == "assistant-current"
        browser._conversation_page = AsyncMock(return_value=page)
        browser._read_turn_surface = AsyncMock(return_value={"messages": [message("assistant-current", "assistant", "long final answer")]})
        snapshot = await browser.inspect(submission)
        assert snapshot.state == "COMPLETE"
        assert snapshot.result_text == "long final answer"
    asyncio.run(run())


def test_confirmed_user_without_assistant_is_still_a_confirmed_send(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(
            Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile", browser_timeout_seconds=0.025,
        ))
        browser._read_turn_surface = AsyncMock(return_value={"messages": [
            message("assistant-old", "assistant", "old answer"), message("user-current", "user", "current question"),
        ]})
        submission = receipt()
        await browser._confirm_submission(SimpleNamespace(), submission)
        assert submission.submission_state == "confirmed"
        assert submission.current_user_message_id == "user-current"
        assert submission.current_assistant_message_id is None
    asyncio.run(run())


def test_turn_observation_has_a_deadline_without_launching_a_driver(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(
            Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile", browser_timeout_seconds=0.025,
        ))
        async def evaluate(_script):
            await asyncio.Event().wait()
        with pytest.raises(BrowserUnresponsiveError, match="deadline"):
            await browser._read_turn_surface(SimpleNamespace(evaluate=evaluate))
        assert browser._scope.unresponsive is True
    asyncio.run(run())


def test_research_frames_and_sources_are_scoped_to_the_current_assistant(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "profile"))
        class FrameStub:
            def __init__(self, **attributes):
                self.__dict__.update(attributes)
        main = FrameStub(url=URL, parent_frame=None)
        def frame(owner, payload):
            element = SimpleNamespace(
                get_attribute=AsyncMock(return_value="Deep research"),
                evaluate=AsyncMock(return_value={
                    "visible": True, "canonical_owner": owner, "owner_ids": [owner],
                    "run_ids": [], "panel_ids": [],
                }),
            )
            body = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
            return FrameStub(
                url="internal://deep-research", parent_frame=main,
                frame_element=AsyncMock(return_value=element), locator=lambda _selector: body,
            ), body
        old, old_body = frame("assistant-old", {"text": "Old report", "sources": ["https://old.example/"], "active": True})
        current, current_body = frame("assistant-current", {
            "text": "Current research report", "sources": ["https://new.example/"], "active": True,
            "complete": True, "completion_evidence": ["completed_status"],
        })
        page = SimpleNamespace(url=URL, main_frame=main, frames=[main, old, current])
        browser._conversation_page = AsyncMock(return_value=page)
        browser._read_turn_surface = AsyncMock(return_value={"messages": [
            message("user-current", "user", "current question"),
            message("assistant-current", "assistant", "Research started", has_research_widget=True),
        ]})
        submission = receipt(confirmed=True)
        running = await browser.inspect(submission)
        assert running.state == "RUNNING"
        assert running.result_text is None
        assert running.sources == ["https://new.example/"]
        old_body.evaluate.assert_not_awaited()
        current_body.evaluate.return_value["active"] = False
        complete = await browser.inspect(submission)
        assert complete.state == "COMPLETE"
        assert complete.result_text == "Current research report"
        assert complete.research_report_complete is True
        assert complete.sources == ["https://new.example/"]
        old_body.evaluate.assert_not_awaited()
    asyncio.run(run())
