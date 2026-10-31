from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatgpt_oracle.browser import BrowserSnapshot, ChatGPTBrowser, SubmitReceipt, _prompt_text_sha256
from chatgpt_oracle.chatgpt_research import merge_research_surfaces, research_association
from chatgpt_oracle.config import Settings


def test_report_completion_requires_positive_evidence_and_active_work_vetoes_it():
    for payload in (
        {"text": "Researching 4 sources"},
        {"text": "Research in progress / 30%", "active": True},
        {"text": "Your report will appear here when the research finishes."},
        {},
        {"text": "An arbitrary iframe page", "complete": True},
    ):
        surface = merge_research_surfaces([payload])
        assert surface["complete"] is False
        assert surface["text"] == ""
    report = {"text": "Completed report\nEvidence and conclusions.", "complete": True,
              "completion_evidence": ["completed_status"], "report_id": "report-current",
              "sources": ["https://source.example/", "https://chatgpt.com/", "javascript:bad"]}
    assert merge_research_surfaces([report])["complete"] is True
    assert merge_research_surfaces([report])["sources"] == ["https://source.example/"]
    for conflicting in ({"active": True}, {"plan_ready": True}, {"stopped": True}):
        surface = merge_research_surfaces([report, conflicting])
        assert surface["complete"] is False and surface["text"] == ""
    assert merge_research_surfaces([report], unassociated=True)["complete"] is False
    other = {**report, "report_id": "report-other"}
    assert merge_research_surfaces([report, other])["complete"] is False
    assert merge_research_surfaces([{**report, "run_id": "run-current"}, {**report, "run_id": "run-other"}])["complete"] is False


def test_portal_ownership_requires_explicit_current_turn_evidence():
    context = {"message_ids": {"assistant-current", "user-current"}, "run_ids": {"run-current"},
               "panel_ids": {"panel-current"}}
    assert research_association({}, **context) == "unknown"
    assert research_association({"owner_ids": ["assistant-current"]}, **context) == "matched"
    assert research_association({"owner_ids": ["assistant-old"]}, **context) == "foreign"
    assert research_association({"owner_ids": ["assistant-current", "assistant-old"]}, **context) == "foreign"
    assert research_association({"run_ids": ["run-current"]}, **context) == "matched"
    assert research_association({"run_ids": ["run-old"]}, **context) == "foreign"
    assert research_association({"owner_ids": ["assistant-current"], "run_ids": ["run-old"]}, **context) == "foreign"
    assert research_association({"panel_ids": ["panel-current"]}, **context) == "matched"
    assert research_association({"canonical_owner": "assistant-old", "run_ids": ["run-current"]}, **context) == "foreign"


def test_foreign_and_ownerless_frame_contents_are_never_read(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "fixture"))
        class Frame:
            def __init__(self, **values): self.__dict__.update(values)
        main = Frame(url="https://chatgpt.com/c/test", parent_frame=None)
        bodies = []
        frames = []
        for owner in ("assistant-old", None, "assistant-current"):
            metadata = {"visible": True, "owner_ids": [owner] if owner else [],
                        "canonical_owner": owner, "title": "Deep research"}
            element = SimpleNamespace(evaluate=AsyncMock(return_value=metadata))
            body = SimpleNamespace(evaluate=AsyncMock(return_value={
                "complete": True, "completion_evidence": ["completed_status"],
                "text": "Matched report", "sources": ["https://current.example/"],
            }))
            bodies.append(body)
            frames.append(Frame(url="internal://deep-research", parent_frame=main,
                                frame_element=AsyncMock(return_value=element), locator=lambda _selector, body=body: body))
        page = SimpleNamespace(main_frame=main, frames=[main, *frames])
        result = await browser._research_frame_surface(page, current_message_ids=["assistant-current"])
        assert result["unassociated"] is True
        assert result["complete"] is False and result["text"] == ""
        bodies[0].evaluate.assert_not_awaited()
        bodies[1].evaluate.assert_not_awaited()
        bodies[2].evaluate.assert_awaited_once()
    asyncio.run(run())


def test_quiet_pending_acknowledgement_is_not_proof_of_cancellation(tmp_path):
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "fixture"))
        current = SubmitReceipt("https://chatgpt.com/c/test", 0, current_user_message_id="user-current", submission_state="confirmed")
        browser._conversation_page = AsyncMock(return_value=object())
        browser.inspect = AsyncMock(return_value=BrowserSnapshot("RUNNING", current.conversation_url, generation_active=False))
        browser._page_has_composer = AsyncMock(return_value=True)
        browser._page_requires_login = AsyncMock(return_value=False)
        assert await browser.request_stop(current) is False
    asyncio.run(run())


@pytest.mark.parametrize("research_expected", [False, True])
def test_research_does_not_hide_current_human_clarification(tmp_path, research_expected):
    async def run():
        browser = ChatGPTBrowser(replace(Settings.from_env(tmp_path), browser_profile_dir=tmp_path / "fixture"))
        page = SimpleNamespace(url="https://chatgpt.com/c/test", frames=[])
        browser._conversation_page = AsyncMock(return_value=page)
        browser._read_turn_surface = AsyncMock(return_value={"messages": [
            {"id": "user-current", "role": "user", "text": "question"},
            {"id": "assistant-current", "role": "assistant", "text": "Please clarify the date range."},
        ]})
        receipt = SubmitReceipt(page.url, 0, current_user_message_id="user-current", submission_state="confirmed",
                                submitted_prompt_sha256=_prompt_text_sha256("question"), research_expected=research_expected)
        snapshot = await browser.inspect(receipt)
        assert snapshot.state == "NEEDS_USER_INPUT"
        assert snapshot.user_input_kind == "content_clarification"
        assert snapshot.clarification_text == "Please clarify the date range."
    asyncio.run(run())
