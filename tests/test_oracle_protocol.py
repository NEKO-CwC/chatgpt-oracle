from __future__ import annotations

import asyncio
import hashlib

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, BrowserTaskError, SubmitReceipt, _resolve_current_turn
from chatgpt_oracle.jobs import JobManager
from chatgpt_oracle.models import Job, public_job, utc_now
from chatgpt_oracle.oracle_protocol import (
    BOOTSTRAP_VERSION, ORIGIN_MARKER, RECOVERY_REMINDER, bootstrap_text, normalized_text_sha256,
)
from chatgpt_oracle.scheduler import IdempotencyConflict
from test_jobs import settings, wait_terminal


class ObservedBrowser:
    """Observe a rendered user/assistant pair, instead of trusting send intent."""

    def __init__(self):
        self.manager = None
        self.calls = []
        self.open_urls = set()
        self.fail_before_send = False
        self.confirm = True

    async def _send(self, url, text, attachments=None, on_upload_verified=None, **kwargs):
        persisted = [j for j in self.manager.store.all() if j.sending_text == text]
        assert persisted and persisted[-1].sending_text_sha256 == hashlib.sha256(text.encode()).hexdigest()
        if self.fail_before_send:
            raise BrowserTaskError("selection failed before sending")
        if attachments and on_upload_verified:
            await on_upload_verified([{**a.metadata.public(), "verified": True} for a in attachments])
        self.calls.append((url, text))
        number = len(self.calls)
        self.open_urls.add(url)
        receipt = SubmitReceipt(url, 0, submitted_prompt_sha256=normalized_text_sha256(text), submission_state="unconfirmed")
        if self.confirm:
            _resolve_current_turn(receipt, [
                {"id": f"u-{number}", "role": "user", "text": text},
                {"id": f"a-{number}", "role": "assistant", "text": "answer"},
            ])
        return receipt

    async def submit(self, mode, prompt, **kwargs):
        return await self._send(f"https://chatgpt.com/c/new-{len(self.calls)}", prompt, **kwargs)

    async def continue_conversation(self, url, prompt, **kwargs):
        return await self._send(url, prompt, **kwargs)

    async def inspect(self, receipt):
        return BrowserSnapshot("COMPLETE", receipt.conversation_url, result_text="answer", generation_active=False)

    async def has_conversation_tab(self, url):
        return url in self.open_urls

    async def close(self):
        pass


def setup(tmp_path):
    browser = ObservedBrowser()
    manager = JobManager(settings(tmp_path), browser)
    browser.manager = manager
    return manager, browser


@pytest.mark.parametrize("attachments", [False, True])
def test_new_and_resumed_turns_freeze_body_and_receipt_before_sending(tmp_path, attachments):
    async def run():
        manager, browser = setup(tmp_path)
        try:
            created = manager.sessions.create("discussion")
            assert manager.public_session(created)["oracle_protocol"]["bootstrap"]["state"] == "unknown"
            handles = []
            if attachments:
                handles = [(await manager.uploads.put("context.txt", "text/plain", b"specific local evidence")).upload_id]
            first = await manager.submit("critic", f"{ORIGIN_MARKER}\n\n{ORIGIN_MARKER}\nfirst question\nquoted: {ORIGIN_MARKER}",
                                         session_id=created.id, request_id="first", upload_ids=handles)
            first = await wait_terminal(manager, first.id)
            sent = browser.calls[0][1]
            assert sent.startswith(ORIGIN_MARKER + "\n") and bootstrap_text() in sent
            assert "first question\nquoted: " + ORIGIN_MARKER in sent
            assert public_job(first)["oracle_protocol"]["bootstrap"]["state"] == "confirmed"
            assert public_job(first)["oracle_protocol"]["bootstrap"]["task_id"] == first.id
            assert first.prompt != sent and first.prompt_sha256 != first.sending_text_sha256
            second = await manager.resume_session(created.id, "counterevidence", request_id="second", mode="critic")
            second = await wait_terminal(manager, second.id)
            assert browser.calls[1] == (first.conversation_url, ORIGIN_MARKER + "\n\ncounterevidence")
            assert second.oracle_bootstrap_task_id == first.id
            assert second.current_user_message_id != first.current_user_message_id
            retry = await manager.submit("critic", first.prompt, session_id=created.id, request_id="first", upload_ids=handles)
            assert retry.id == first.id and retry.sending_text == sent and len(browser.calls) == 2
            with pytest.raises(IdempotencyConflict):
                await manager.submit("critic", "changed request", session_id=created.id, request_id="first")
        finally:
            await manager.stop()
    asyncio.run(run())


def test_mode_change_alias_and_recovery_reuse_confirmed_bootstrap(tmp_path):
    async def run():
        manager, browser = setup(tmp_path)
        try:
            first = await manager.submit("consult", "first", session_id="original")
            first = await wait_terminal(manager, first.id)
            alias = manager.sessions.touch("alias", conversation_url=first.conversation_url)
            assert manager.public_session(alias)["oracle_protocol"]["bootstrap"]["task_id"] == first.id
            browser.open_urls.clear()
            follow = await manager.resume_session("alias", "new argument", mode="critic")
            follow = await wait_terminal(manager, follow.id)
            assert bootstrap_text() not in follow.sending_text
            assert RECOVERY_REMINDER in follow.sending_text and follow.oracle_mode_overlay_included
            assert follow.oracle_bootstrap_task_id == first.id
            assert follow.mode == "critic"
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("path", ["continue", "resume"])
def test_legacy_url_never_claims_or_injects_bootstrap(tmp_path, path):
    async def run():
        manager, browser = setup(tmp_path)
        try:
            old = Job.create("critic", "unmarked historical user content", session_id="legacy")
            old.conversation_url = "https://chatgpt.com/c/legacy"
            old.state = "COMPLETE"
            manager.store.save(old)
            manager.sessions.touch("legacy", conversation_url=old.conversation_url, last_task_id=old.id)
            if path == "continue":
                follow = await manager.continue_task(old.id, "follow-up", request_id="legacy-follow")
            else:
                follow = await manager.resume_session("legacy", "follow-up", request_id="legacy-follow")
            follow = await wait_terminal(manager, follow.id)
            assert bootstrap_text() not in follow.sending_text and RECOVERY_REMINDER not in follow.sending_text
            assert follow.sending_text.startswith(ORIGIN_MARKER + "\n")
            assert public_job(follow)["oracle_protocol"]["bootstrap"]["state"] == "unknown"
            assert manager.store.get(old.id).prompt == old.prompt
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("before_send", [True, False])
def test_prepared_or_ambiguous_send_is_not_bootstrap_delivery(tmp_path, before_send):
    async def run():
        manager, browser = setup(tmp_path)
        browser.fail_before_send = before_send
        browser.confirm = False
        try:
            job = await manager.submit("consult", "question", session_id="uncertain", request_id="uncertain")
            job = await wait_terminal(manager, job.id)
            assert job.state == "ERROR"
            assert public_job(job)["oracle_protocol"]["bootstrap"]["state"] == "prepared"
            assert manager.public_session(manager.sessions.get("uncertain"))["oracle_protocol"]["bootstrap"]["state"] == "unknown"
            retried = await manager.submit("consult", "question", session_id="uncertain", request_id="uncertain")
            assert retried.id == job.id and len(browser.calls) == (0 if before_send else 1)
        finally:
            await manager.stop()
    asyncio.run(run())


def test_original_resume_intent_survives_later_inherited_mode_and_quality(tmp_path):
    async def run():
        manager, browser = setup(tmp_path)
        try:
            first = await manager.submit("consult", "initial", session_id="intent")
            await wait_terminal(manager, first.id)
            follow = await manager.resume_session("intent", "same original question", request_id="original-follow")
            follow = await wait_terminal(manager, follow.id)
            changed = await manager.resume_session("intent", "later question", mode="critic", quality="pro", request_id="later")
            await wait_terminal(manager, changed.id)
            retry = await manager.resume_session("intent", "same original question", request_id="original-follow")
            assert retry.id == follow.id and retry.sending_text == follow.sending_text and len(browser.calls) == 3
            with pytest.raises(IdempotencyConflict):
                await manager.resume_session("intent", "same original question", mode="critic", request_id="original-follow")
        finally:
            await manager.stop()
    asyncio.run(run())


def test_confirmed_restart_observes_original_frozen_turn_without_resending(tmp_path):
    async def run():
        manager, browser = setup(tmp_path)
        try:
            first = await manager.submit("consult", "only send", session_id="recover", request_id="original")
            first = await wait_terminal(manager, first.id)
            restored = Job.from_dict(first.as_dict())
            restored.state = "RUNNING"
            manager.store.save(restored)
            await manager._process_body(restored)
            assert restored.state == "COMPLETE" and len(browser.calls) == 1
            assert restored.sending_text == first.sending_text
            assert public_job(restored)["oracle_protocol"]["bootstrap"]["state"] == "confirmed"
        finally:
            await manager.stop()
    asyncio.run(run())


def test_mismatched_or_legacy_receipts_never_claim_bootstrap_delivery(tmp_path):
    async def run():
        manager, browser = setup(tmp_path)
        try:
            first = await manager.submit("consult", "question", session_id="bad-hash")
            first = await wait_terminal(manager, first.id)
            first.submitted_prompt_sha256 = normalized_text_sha256(first.prompt)
            assert public_job(first)["oracle_protocol"]["bootstrap"]["state"] == "prepared"
            old = Job.from_dict({"id": "cgo_legacy", "mode": "consult", "prompt": "old", "submission_state": "confirmed", "current_user_message_id": "u-old"})
            assert public_job(old)["oracle_protocol"]["bootstrap"]["state"] == "unknown"
        finally:
            await manager.stop()
    asyncio.run(run())


def test_cancel_during_protocol_preparation_is_not_overwritten_or_sent(tmp_path):
    async def run():
        manager, browser = setup(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            first = await manager.submit("consult", "first", session_id="cancel-prepare")
            await wait_terminal(manager, first.id)
            async def has_tab(url):
                entered.set()
                await release.wait()
                return True
            browser.has_conversation_tab = has_tab
            follow = await manager.resume_session("cancel-prepare", "never send", request_id="cancel-before-send")
            await entered.wait()
            await manager.cancel(follow.id)
            release.set()
            follow = await wait_terminal(manager, follow.id)
            assert follow.state == "CANCELLED" and follow.cancel_requested_at
            assert len(browser.calls) == 1 and follow.current_user_message_id is None
        finally:
            release.set()
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["content_clarification", "human_decision", "manual_browser"])
def test_current_clarification_is_readable_without_claiming_complete_and_can_continue(tmp_path, kind):
    async def run():
        manager, browser = setup(tmp_path)
        question = "Please clarify: local observation?" if kind == "content_clarification" else "Please clarify: which goal do you want?"
        original_inspect = browser.inspect
        async def inspect(receipt):
            if len(browser.calls) == 1:
                return BrowserSnapshot("NEEDS_USER_INPUT", receipt.conversation_url, user_action="input needed",
                                       user_input_kind="content_clarification" if kind != "manual_browser" else kind,
                                       clarification_text=question if kind != "manual_browser" else None, generation_active=False)
            return await original_inspect(receipt)
        browser.inspect = inspect
        try:
            first = await manager.submit("consult", "original question", session_id="clarify")
            for _ in range(200):
                current = manager.get(first.id)
                if current.state == "NEEDS_USER_INPUT":
                    break
                await asyncio.sleep(.01)
            assert current.state == "NEEDS_USER_INPUT" and manager.result(current.id)[1] is None
            assert current.clarification_text == (question if kind != "manual_browser" else None)
            if kind == "content_clarification":
                follow = await manager.resume_session("clarify", "Observed locally: 4 resident pages", request_id="answer-current-question")
                follow = await wait_terminal(manager, follow.id)
                assert follow.state == "COMPLETE" and follow.current_user_message_id != current.current_user_message_id
                assert len(browser.calls) == 2 and browser.calls[1][1] == ORIGIN_MARKER + "\n\nObserved locally: 4 resident pages"
            else:
                # Reading a human-only decision or a login prompt never
                # authorizes Main to invent the answer or resubmit the task.
                assert len(browser.calls) == 1
        finally:
            await manager.stop()
    asyncio.run(run())
