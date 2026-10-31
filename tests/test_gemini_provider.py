from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, SubmitReceipt
from chatgpt_oracle.config import Settings
from chatgpt_oracle.file_store import FileTransferError
from chatgpt_oracle.gemini_browser import GeminiBrowser, GeminiTaskError, identify_messages, matched_turn, research_turn
from chatgpt_oracle.jobs import ExportRequestError, JobManager
from chatgpt_oracle.oracle_protocol import normalized_text_sha256
from chatgpt_oracle.models import Job
from chatgpt_oracle.providers import CHATGPT, GEMINI


def gemini_settings(tmp_path):
    return replace(Settings.from_env(tmp_path, provider="gemini"), consult_poll_seconds=.01,
                   gemini_account_indexes=(0,),
                   browser_profile_dir=tmp_path / "profile",
                   critic_poll_seconds=.01, research_poll_seconds=.01,
                   browser_timeout_seconds=1, consult_timeout_seconds=1, research_timeout_seconds=1)


class FakeGemini:
    requires_turn_receipts = True
    continuation_accepts_mode = True

    def __init__(self, *, plan=False, start_fails=False):
        self.sent = []
        self.continued = []
        self.start_calls = 0
        self.plan = plan
        self.start_fails = start_fails

    async def submit(self, mode, prompt, **kwargs):
        self.sent.append((mode, prompt, kwargs))
        return SubmitReceipt("https://gemini.google.com/app/test", 0,
                             resolved_model="4 Argon", resolved_reasoning_effort=kwargs["quality"].title(),
                             selection_verified_at="2026-10-14T00:00:00Z", selection_source="visible_picker",
                             submitted_prompt_sha256=normalized_text_sha256(prompt),
                             current_user_message_id=f"u{len(self.sent)}", submission_state="confirmed",
                             submission_verified_at="2026-10-14T00:00:00Z", research_expected=self.plan)

    async def continue_conversation(self, url, prompt, **kwargs):
        self.continued.append(kwargs)
        options = dict(kwargs)
        mode = options.pop("mode", "consult")
        return await self.submit(mode, prompt, **options)

    async def inspect(self, receipt):
        if self.plan and self.start_calls == 0:
            return BrowserSnapshot("RESEARCH_PLAN_READY", receipt.conversation_url, result_text="a plan, not a report")
        receipt.current_assistant_message_id = "a1"
        return BrowserSnapshot("COMPLETE", receipt.conversation_url, result_text="answer")

    async def start_research(self, receipt):
        self.start_calls += 1
        assert receipt.research_start_state == "requested"
        if self.start_fails:
            raise RuntimeError("ambiguous start")

    async def close(self):
        pass


async def finished(manager, job):
    for _ in range(150):
        current = manager.get(job.id)
        if current.state in {"COMPLETE", "ERROR", "CANCELLED", "MODEL_UNAVAILABLE"}:
            return current
        await asyncio.sleep(.01)
    raise AssertionError(manager.public(job.id))


def test_provider_configuration_isolated_without_environment_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_ORACLE_ROOT", str(tmp_path / "chatgpt"))
    monkeypatch.setenv("CHATGPT_ORACLE_BEARER_TOKEN", "chatgpt-test-token")
    monkeypatch.setenv("GEMINI_ORACLE_ROOT", str(tmp_path / "gemini"))
    before = dict(os.environ)
    gemini = Settings.from_env(provider="gemini")
    chatgpt = Settings.from_env(provider="chatgpt")
    assert os.environ == before
    assert gemini.provider == "gemini" and chatgpt.provider == "chatgpt"
    assert gemini.jobs_dir != chatgpt.jobs_dir
    assert gemini.sessions_dir != chatgpt.sessions_dir
    assert gemini.port == 8788 and gemini.browser_cdp_url == "http://127.0.0.1:9333"
    assert gemini.bearer_token is None
    assert gemini.file_store_max_file_bytes == 100 * 1024 * 1024


@pytest.mark.parametrize("value", ["https://chatgpt.com/c/a", "https://gemini.google.com/app/", "https://gemini.google.com.evil.test/app/a", "https://user@ gemini.google.com/app/a", "https://gemini.google.com:9443/app/a"])
def test_gemini_rejects_foreign_or_nonconversation_urls(value):
    with pytest.raises(ValueError):
        GEMINI.canonical_url(value)


def test_provider_url_keys_ignore_query_and_fragment():
    assert GEMINI.canonical_url("https://gemini.google.com/app/abc/?x=1#foo") == "https://gemini.google.com/app/abc"
    assert CHATGPT.canonical_url("https://chatgpt.com/c/abc") == "https://chatgpt.com/c/abc"


def test_turn_matching_uses_content_and_predecessor_not_position_or_count():
    first = identify_messages([
        {"role": "user", "text": "same", "dom_id": "user-query-content-99"},
        {"role": "assistant", "id": "r-old", "text": "old"},
        {"role": "user", "text": "same", "dom_id": "user-query-content-100"},
        {"role": "assistant", "id": "r-new", "text": "new"},
    ])
    rebuilt = identify_messages([{**m, "dom_id": "different"} for m in [
        {"role": "user", "text": "same"}, {"role": "assistant", "id": "r-old", "text": "old"},
        {"role": "user", "text": "same"}, {"role": "assistant", "id": "r-new", "text": "new"}]])
    assert [m["id"] for m in first] == [m["id"] for m in rebuilt]
    receipt = SubmitReceipt("https://gemini.google.com/app/test", 1,
                            user_message_ids_before=[first[0]["id"]],
                            assistant_message_ids_before=["r-old"],
                            submitted_prompt_sha256=normalized_text_sha256("same"))
    user, replies = matched_turn(first, receipt)
    assert user["id"] == first[2]["id"] and replies[0]["id"] == "r-new"
    receipt.current_user_message_id = user["id"]
    wrong = identify_messages([{ "role": "user", "text": "a different prompt"},
                               {"role": "assistant", "id": "r-other", "text": "stale"}])
    assert matched_turn(wrong, receipt) == (None, [])


def test_duplicate_unconfirmed_matches_are_ambiguous():
    messages = identify_messages([{ "role": "user", "text": "duplicate"},
                                  {"role": "assistant", "id": "a", "text": "old"},
                                  {"role": "user", "text": "duplicate"}])
    receipt = SubmitReceipt("https://gemini.google.com/app/test", 0,
                            submitted_prompt_sha256=normalized_text_sha256("duplicate"))
    assert matched_turn(messages, receipt) == (None, [])


def test_menu_readback_uses_atomic_snapshot_and_selected_class(tmp_path):
    from test_gemini_selection import run_gemini_page, gemini_picker
    async def check(browser, page):
        options, model, efforts, selected = await browser._options(page)
        assert model == "4 Argon" and selected["label"] == "High"
        assert next(item for item in options if item["name"] == "3.1 Pro")["disabled"] is True
        assert not await page.locator('[role="menu"]:visible').count()
    run_gemini_page(tmp_path, gemini_picker(disabled_pro=True), check)


def test_stop_does_not_touch_a_later_human_turn(tmp_path):
    async def run():
        browser = GeminiBrowser(gemini_settings(tmp_path))
        messages = identify_messages([{"role": "user", "text": "ours"},
                                      {"role": "assistant", "id": "a1", "text": "answer"},
                                      {"role": "user", "text": "human interleaved"}])
        receipt = SubmitReceipt("https://gemini.google.com/app/test", 0,
                                current_user_message_id=messages[0]["id"],
                                submitted_prompt_sha256=normalized_text_sha256("ours"))

        async def page(_):
            return object()  # No click methods: touching the wrong page fails the test.

        async def surface(_):
            return {"messages": messages, "active": True}

        browser._managed_page, browser._surface = page, surface
        assert await browser.request_stop(receipt) is False
    asyncio.run(run())


def research_messages():
    return identify_messages([
        {"role": "user", "text": "original research question"},
        {"role": "assistant", "id": "plan", "text": "plan", "complete": False, "plan_ready": False},
        {"role": "user", "text": "开始研究"},
        {"role": "assistant", "id": "report", "text": "Starting research...", "complete": False,
         "plan_ready": False, "has_research_card": True, "research_processing": True},
    ])


def research_receipt(messages):
    return SubmitReceipt("https://gemini.google.com/app/test", 0,
                         current_user_message_id=messages[0]["id"],
                         submitted_prompt_sha256=normalized_text_sha256("original research question"),
                         research_expected=True, research_start_state="requested",
                         resolved_model="4 Argon", resolved_reasoning_effort="High")


def test_research_provider_turn_preserves_original_prompt_identity():
    messages = research_messages()
    receipt = research_receipt(messages)
    original_identity = receipt.current_user_message_id, receipt.submitted_prompt_sha256
    user, replies = research_turn(messages, receipt)
    assert user["id"] == messages[2]["id"] and replies[-1]["id"] == "report"
    assert receipt.research_start_state == "confirmed"
    assert (receipt.current_user_message_id, receipt.submitted_prompt_sha256) == original_identity
    assert receipt.research_assistant_message_id == "report"


def test_research_quota_banner_wins_over_processing_placeholder(tmp_path):
    async def run():
        browser = GeminiBrowser(gemini_settings(tmp_path))
        messages = research_messages()
        receipt = research_receipt(messages)
        async def page(_):
            return object()
        async def ready(_):
            return None
        async def surface(_):
            return {"messages": messages, "active": True, "alerts": ["You’ve reached your current usage limit"], "research_report": None}
        browser._managed_page, browser._ready, browser._surface = page, ready, surface
        with pytest.raises(GeminiTaskError) as error:
            await browser.inspect(receipt)
        assert error.value.code == "UPSTREAM_LIMIT"
        assert receipt.research_start_state == "confirmed"
    asyncio.run(run())


def test_research_placeholder_is_not_final_report_and_completed_panel_is(tmp_path):
    class Page:
        url = "https://gemini.google.com/app/test"
    class Picker:
        async def get_attribute(self, _):
            return "Open mode picker, currently Argon High"
    async def run():
        browser = GeminiBrowser(gemini_settings(tmp_path))
        messages = research_messages()
        receipt = research_receipt(messages)
        state = {"messages": messages, "active": True, "alerts": [], "research_report": None}
        async def page(_):
            return Page()
        async def ready(_):
            return None
        async def surface(_):
            return state
        browser._managed_page, browser._ready, browser._surface = page, ready, surface
        browser._picker = lambda _: Picker()
        result = await browser.inspect(receipt)
        assert result.state == "RUNNING" and result.result_text is None
        state["active"] = False
        state["research_report"] = {"text": "Actual report with [source](https://support.google.com)", "sources": ["https://support.google.com"], "busy": False}
        result = await browser.inspect(receipt)
        assert result.state == "COMPLETE" and result.result_text.startswith("Actual report")
        assert result.current_user_message_id == messages[0]["id"]
    asyncio.run(run())


def test_research_cancel_confirmation_is_scoped_and_clicked_once(tmp_path):
    async def run():
        browser = GeminiBrowser(gemini_settings(tmp_path))
        messages = research_messages()
        receipt = research_receipt(messages)
        research_turn(messages, receipt)
        state = {"active": True, "dialog": False, "clicks": []}

        class Button:
            def __init__(self, kind): self.kind = kind
            @property
            def first(self): return self
            async def count(self): return 1
            async def click(self):
                state["clicks"].append(self.kind)
                if self.kind == "stop": state["dialog"] = True
                else: state.update(active=False, dialog=False)

        class Dialog:
            async def count(self): return int(state["dialog"])
            async def is_visible(self): return state["dialog"]
            async def wait_for(self, **_): assert not state["dialog"]
            def get_by_role(self, role, name):
                assert role == "button" and name.search("Yes, cancel")
                return Button("confirm")

        class Page:
            def get_by_role(self, role, name):
                if role == "dialog":
                    assert name.search("Cancel research?")
                    return Dialog()
                return Button("stop")

        async def page(_): return Page()
        async def surface(_): return {"messages": messages, "active": state["active"]}
        browser._managed_page, browser._surface = page, surface
        assert await browser.request_stop(receipt) is True
        assert state["clicks"] == ["stop", "confirm"]
    asyncio.run(run())


def test_visible_quota_prevents_a_new_submission(tmp_path):
    async def run():
        browser = GeminiBrowser(gemini_settings(tmp_path))
        class Page:
            async def bring_to_front(self):
                pass
            async def evaluate(self, _):
                return {"image_count": 0, "removals": []}
        async def draft(*_): return None
        async def surface(_): return {"alerts": ["You’ve reached your current usage limit"], "active": False, "messages": []}
        from unittest.mock import AsyncMock
        browser._draft, browser._surface = draft, surface
        browser._dismiss_discovery = AsyncMock()
        with pytest.raises(GeminiTaskError) as error:
            await browser._prepare_send(Page(), "never send this", model=None, quality="high", reasoning_effort=None,
                                allow_downgrade=False, attachments=[], on_upload_verified=None, research=False)
        assert error.value.code == "UPSTREAM_LIMIT"
    asyncio.run(run())


def test_provider_display_does_not_rewrite_model_clarifications(tmp_path):
    manager = JobManager(gemini_settings(tmp_path), FakeGemini())
    job = Job.create("consult", "compare the products")
    job.user_action = job.clarification_text = "Do you want ChatGPT or Gemini? Use chatgpt_example as the label."
    job.error = "The requested model ChatGPT-example is not available."
    manager.store.save(job)
    public = manager.public(job.id)
    assert public["user_action"] == job.user_action
    assert public["clarification_text"] == job.clarification_text
    assert public["error"] == job.error


def test_gemini_defaults_bootstrap_and_continuation(tmp_path):
    async def run():
        browser = FakeGemini()
        manager = JobManager(gemini_settings(tmp_path), browser)
        try:
            job = await manager.submit("consult", "first", session_id="shared-name")
            assert (await finished(manager, job)).state == "COMPLETE"
            assert browser.sent[0][2]["quality"] == "high"
            assert browser.sent[0][2]["dedicated_page"] is True
            assert manager._canonical_conversation_url(manager.get(job.id).conversation_url).startswith("https://gemini.google.com/app/")
            follow = await manager.resume_session("shared-name", "follow")
            assert (await finished(manager, follow)).state == "COMPLETE"
            assert browser.continued[0]["mode"] == "consult"
            assert manager.get(follow.id).oracle_bootstrap_included is False
            assert manager.public(follow.id)["selection_evidence"]["generation_model"] == "unknown"
            assert manager.public(follow.id)["provider"] == "gemini"
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["submit", "continue", "resume"])
def test_zip_rejected_before_admission_on_every_path(tmp_path, operation):
    async def run():
        manager = JobManager(gemini_settings(tmp_path), FakeGemini())
        try:
            original = await manager.submit("consult", "first", session_id="input-owner")
            await finished(manager, original)
            metadata = await manager.uploads.put("context.zip", "application/zip", b"fake archive")
            before = len(manager.store.all())
            with pytest.raises(FileTransferError) as error:
                if operation == "submit":
                    await manager.submit("consult", "zip", upload_ids=[metadata.upload_id])
                elif operation == "continue":
                    await manager.continue_task(original.id, "zip", upload_ids=[metadata.upload_id])
                else:
                    await manager.resume_session("input-owner", "zip", upload_ids=[metadata.upload_id])
            assert error.value.code == "UNSUPPORTED_UPLOAD_TYPE"
            assert len(manager.store.all()) == before
        finally:
            await manager.stop()
    asyncio.run(run())


def test_reused_zip_file_ref_rejected_and_wrong_quality_not_translated(tmp_path):
    async def run():
        manager = JobManager(gemini_settings(tmp_path), FakeGemini())
        try:
            manager.sessions.create("file-owner")
            transfer = manager.files.create_upload(session_id="file-owner", filename="a.zip", size=1, mime_type="application/zip")
            manager.files.append_chunk(transfer["transfer_id"], "file-owner", 0, b"x")
            with pytest.raises(FileTransferError, match="ZIP"):
                await manager.submit("consult", "zip", session_id="file-owner", file_refs=[transfer["file_ref"]])
            with pytest.raises(ValueError, match="quality"):
                await manager.submit("consult", "wrong alias", quality="extra_high")
            with pytest.raises(ExportRequestError) as error:
                await manager.start_attachment_download(session_id="file-owner", export_id="missing", attachment_id="missing")
            assert error.value.code == "UNSUPPORTED_CAPABILITY"
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("start_fails", [False, True])
def test_research_plan_is_not_result_and_start_is_not_repeated(tmp_path, start_fails):
    async def run():
        browser = FakeGemini(plan=True, start_fails=start_fails)
        manager = JobManager(gemini_settings(tmp_path), browser)
        try:
            job = await manager.submit("research", "test research", session_id="research")
            current = await finished(manager, job)
            assert browser.start_calls == 1
            if start_fails:
                assert current.state == "ERROR" and current.error_code == "RESEARCH_START_UNCONFIRMED"
                assert manager.result(job.id)[1] is None
            else:
                assert current.state == "COMPLETE" and current.research_start_state == "confirmed"
                assert "a plan, not a report" not in manager.result(job.id)[1]
        finally:
            await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,count", [("chatgpt", 29), ("gemini", 28)])
def test_server_registers_only_provider_tools_in_isolated_process(tmp_path, provider, count):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CHATGPT_", "GEMINI_"))}
    env.update(ORACLE_PROVIDER=provider)
    env[f"{provider.upper()}_ORACLE_ROOT"] = str(tmp_path)
    program = "import asyncio,json; from chatgpt_oracle.server import mcp,chatgpt_capabilities; print(json.dumps({'tools':asyncio.run(mcp.list_tools()),'caps':asyncio.run(chatgpt_capabilities(scope='service'))},default=lambda x:x.model_dump()))"
    process = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True, text=True, check=True)
    record = json.loads(process.stdout)
    tools = record["tools"]
    names = [tool["name"] for tool in tools]
    assert len(names) == count
    assert all(name.startswith(provider + "_") for name in names)
    if provider == "gemini":
        assert "gemini_account_select" in names
        assert record["caps"]["provider_capabilities"]["accounts"]["selection_tool"] == "gemini_account_select"
        assert "gemini_export_attachment_handle" not in names
        assert "gemini_file_download_start" not in names
        assert record["caps"]["oracle_protocol"]["account_page_recovery"]["max_refreshes_per_operation"] == 0
        assert record["caps"]["source_attachment_materialization"]["enabled"] is False
        assert record["caps"]["provider_capabilities"]["zip_inputs"] is False
    else:
        assert record["caps"]["oracle_protocol"]["account_page_recovery"]["max_refreshes_per_operation"] == 2


def test_account_selection_handler_persists_and_exposes_choice_without_browser_io(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CHATGPT_", "GEMINI_"))}
    env.update(ORACLE_PROVIDER="gemini", GEMINI_ORACLE_ROOT=str(tmp_path))
    program = """
import asyncio,json
from unittest.mock import AsyncMock
from chatgpt_oracle.server import manager,gemini_account_select,chatgpt_capabilities
async def main():
    manager.browser.start=AsyncMock(side_effect=AssertionError('selection must not connect to Chrome'))
    selected=await gemini_account_select(0)
    assert selected['accepted'] and selected['strict'] and not selected['fallback_allowed']
    caps=await chatgpt_capabilities(scope='service')
    assert caps['provider_capabilities']['accounts']['explicit_selection']['account_index']==0
    rejected=await gemini_account_select(2)
    assert not rejected['accepted'] and rejected['error_code']=='INVALID_ACCOUNT_INDEX'
    assert await manager.browser._account_order()==[0]
    relaxed=await gemini_account_select(1,strict=False)
    assert relaxed['fallback_allowed'] and await manager.browser._account_order()==[1,0]
    reset=await gemini_account_select(None)
    assert reset['accepted'] and reset['mode']=='automatic'
    assert (await chatgpt_capabilities(scope='service'))['provider_capabilities']['accounts']['explicit_selection'] is None
    manager.browser.start.assert_not_awaited()
    print(json.dumps({'selected':selected,'reset':reset,'rejected':rejected}))
asyncio.run(main())
"""
    process = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True, text=True, check=True)
    assert json.loads(process.stdout)["selected"]["home_url"].startswith("https://gemini.google.com/u/0/app")
