from __future__ import annotations

from dataclasses import replace
from html import escape

import pytest

from chatgpt_oracle.browser import BrowserTaskError, SubmitReceipt, _prompt_text_sha256
from test_oracle_browser_boundaries import run_dom


QUESTION = "Local research fixture question"
ACK = "I have started the research."
REPORT = ('<div role="status">Research completed</div><nav><a href="https://ui.example/">Help</a></nav>'
          '<article data-research-report data-report-id="report-current"><h1>Research findings</h1>'
          '<p>Evidence A supports conclusion B. <a href="https://source.example/">Source A</a></p></article>'
          '<button>Copy report</button>')


def frame(content: str, **attributes) -> str:
    attrs = " ".join(f'{name}="{escape(str(value), quote=True)}"' for name, value in attributes.items())
    return f'<iframe title="Deep research" style="width:600px;height:300px" srcdoc="{escape(content, quote=True)}" {attrs}></iframe>'


def turn(assistant: str, *, extra: str = "", prefix: str = "", assistant_attributes: str = "") -> str:
    return (f'<main>{prefix}<div data-message-author-role="user" data-message-id="user-current">{QUESTION}</div>'
            f'<div data-message-author-role="assistant" data-message-id="assistant-current" {assistant_attributes}>{assistant}</div>'
            f'{extra}<textarea placeholder="Message ChatGPT"></textarea></main>')


def receipt(page, *, expected=False):
    return SubmitReceipt(page.url, 0, current_user_message_id="user-current", submission_state="confirmed",
                         submitted_prompt_sha256=_prompt_text_sha256(QUESTION), research_expected=expected)


async def set_document(page, body):
    await page.set_content(body)
    for child in page.frames[1:]:
        await child.wait_for_load_state("domcontentloaded")


def test_progress_placeholders_and_delayed_frames_cannot_complete(tmp_path):
    async def check(browser, page):
        cases = [
            '<div role="status">Researching 4 sources</div>',
            '<div>Research in progress</div><div role="progressbar" aria-label="Research progress" aria-valuenow="30" aria-valuemax="100">30%</div>',
            '<div>Your report will appear here when the research finishes.</div>',
            '',
            '<h1>Research report</h1><p>Report will appear here when ready.</p>',
            '<div role="status">Research completed</div><article data-research-report>Your report will appear here when the research finishes.</article>',
            '<div role="status">Research completed</div><article data-research-report><h1>Research report</h1><p>Your report will appear here when the research finishes.</p></article>',
            '<article data-research-report data-state="complete"><p>Loading...</p></article>',
            '<article data-research-report data-state="complete"><p>100%</p><p>4 sources</p></article>',
            REPORT.replace('Research completed', 'Researching 4 sources'),
        ]
        for content in cases:
            await set_document(page, turn(ACK + frame(content)))
            current = receipt(page)
            for _ in range(2):
                snapshot = await browser.inspect(current)
                assert snapshot.state == "RUNNING", (content, snapshot)
                assert snapshot.result_text is None
                assert snapshot.research_observed is True
                assert snapshot.research_report_complete is False
        # The same already-matched, initially empty iframe can load a report on
        # a later poll. Waiting never creates or sends another user turn.
        await page.locator("iframe").evaluate("(node, html) => node.srcdoc = html", REPORT)
        await page.frames[1].wait_for_load_state("domcontentloaded")
        ready = await browser.inspect(current)
        assert ready.state == "COMPLETE"
        assert "Evidence A" in ready.result_text
        assert ready.sources == ["https://source.example/"]
        for acknowledgement in ("I've started the research. It may take a few minutes.", "研究已开始。", "深度研究已启动。"):
            await set_document(page, turn(acknowledgement))
            delayed = await browser.inspect(receipt(page))
            assert delayed.state == "RUNNING" and delayed.result_text is None
    run_dom(tmp_path, turn(""), check)


def test_portal_ownership_and_report_only_extraction(tmp_path):
    async def check(browser, page):
        # An ownerless portal is an ambiguity blocker, even if it contains a
        # complete old report. None of its text or sources may become output.
        await set_document(page, turn(ACK, extra=frame(REPORT)))
        unknown = await browser.inspect(receipt(page))
        assert unknown.state == "RUNNING" and unknown.result_text is None
        assert unknown.research_state == "unassociated"
        assert unknown.sources == []
        # Ownership may be an explicit message ID, an ID referenced by the
        # current message's aria-controls, or a current research run ID.
        variants = [
            (ACK, frame(REPORT, **{"data-owner-message-id": "assistant-current"}), ""),
            ('<button aria-controls="research-panel-current" aria-expanded="true">Open report</button>',
             '<section id="research-panel-current">' + frame(REPORT) + '</section>', ""),
            (ACK, frame(REPORT, **{"data-research-run-id": "run-current"}), 'data-research-run-id="run-current"'),
        ]
        old = ('<div data-message-author-role="user" data-message-id="user-old">Old question</div>'
               '<div data-message-author-role="assistant" data-message-id="assistant-old">' +
               frame('<button>Stop research</button><article data-research-report data-state="complete"><p>Old report.</p><a href="https://old.example/">Old source</a></article>') + '</div>')
        for assistant, portal, attrs in variants:
            await set_document(page, turn(assistant, extra=portal, prefix=old, assistant_attributes=attrs))
            current = receipt(page)
            snapshot = await browser.inspect(current)
            assert snapshot.state == "COMPLETE", snapshot
            assert snapshot.research_report_complete is True
            assert snapshot.research_observed is True  # No requested research mode needed.
            assert snapshot.research_report_id == "report-current"
            assert snapshot.result_text == "Research findings\nEvidence A supports conclusion B. Source A"
            assert snapshot.sources == ["https://source.example/"]
            assert "Help" not in snapshot.result_text and "Copy report" not in snapshot.result_text
        # A non-iframe portal uses exactly the same ownership and report rules.
        panel = ('<section data-research-panel data-owner-message-id="assistant-current">' + REPORT + '</section>')
        await set_document(page, turn(ACK, extra=panel, prefix=old))
        snapshot = await browser.inspect(receipt(page))
        assert snapshot.state == "COMPLETE" and snapshot.sources == ["https://source.example/"]
        await set_document(page, turn(ACK, extra=panel.replace('<section data-research-panel', '<section role="dialog" aria-label="Deep research" data-research-panel'), prefix=old))
        snapshot = await browser.inspect(receipt(page))
        assert snapshot.state == "COMPLETE" and snapshot.user_input_kind is None
        # A reused global panel ID controlled by two turns is not proof that
        # its currently displayed report belongs to this request.
        shared_old = '<div data-message-author-role="assistant" data-message-id="assistant-old"><button aria-controls="shared-panel">Open old report</button></div>'
        await set_document(page, turn('<button aria-controls="shared-panel">Open report</button>', prefix=shared_old,
                                      extra='<section id="shared-panel">' + frame(REPORT) + '</section>'))
        ambiguous = await browser.inspect(receipt(page))
        assert ambiguous.state == "RUNNING" and ambiguous.sources == []
        assert ambiguous.research_state == "unassociated"
        # Runtime completion plus report prose is readable. A generated
        # heading alone is deliberately covered as pending in semantic tests.
        await set_document(page, turn(ACK + frame('<div role="status">Research completed</div><h1>Completed diagnostic report</h1><p>Observation A and result B.</p>')))
        snapshot = await browser.inspect(receipt(page))
        assert snapshot.state == "COMPLETE"
        assert snapshot.result_text == "Completed diagnostic report\nObservation A and result B."
        await set_document(page, turn(ACK + frame('<div role="status">Research completed</div><h1>Completed diagnostic report</h1><p>Researching the archive revealed supporting evidence.</p>')))
        prose = await browser.inspect(receipt(page))
        assert prose.state == "COMPLETE"  # A prose sentence beginning Researching is not an activity label.
    run_dom(tmp_path, turn(""), check)


def test_plan_start_requires_durable_one_use_intent_and_is_turn_scoped(tmp_path):
    async def check(browser, page):
        plan = ('<section data-research-plan><h2>Research plan</h2><button id="current-start">Start research</button></section>')
        old = ('<div data-message-author-role="assistant" data-message-id="assistant-old">'
               '<button onclick="window.oldStarts++">Start research</button></div>')
        script = '''<script>window.starts=0; window.oldStarts=0;
          document.querySelector('#current-start').onclick=()=>{
            window.starts++;
            const node=document.createElement('iframe'); node.title='Deep research'; node.style='width:600px;height:200px';
            node.srcdoc='<div role="status">Researching 4 sources</div><button>Stop research</button>';
            document.querySelector('[data-research-plan]').replaceWith(node);
          };</script>'''
        await set_document(page, turn(plan, prefix=old, extra='<button onclick="window.otherStarts++">Start</button>') + script)
        current = receipt(page, expected=True)
        ready = await browser.inspect(current)
        assert ready.state == "RESEARCH_PLAN_READY" and ready.research_plan_ready is True
        assert await browser._start_research_plan_if_present(page) is False
        with pytest.raises(BrowserTaskError, match="persisted"):
            await browser.start_research(current)
        assert await page.evaluate("window.starts") == 0
        current.research_start_state = "requested"  # The jobs store writes this before the Web action.
        await browser.start_research(current)
        assert current.research_start_state == "confirmed"
        assert await page.evaluate("window.starts") == 1
        assert await page.evaluate("window.oldStarts") == 0
        with pytest.raises(BrowserTaskError, match="one-use"):
            await browser.start_research(current)
        assert await page.evaluate("window.starts") == 1
        # A plan can start on the page before the reserved action runs. The
        # existing matched transition is acknowledged without another click.
        await set_document(page, turn(ACK + frame('<div role="status">Researching 4 sources</div><button>Stop research</button>')))
        raced = receipt(page, expected=True)
        await browser.inspect(raced)
        raced.research_start_state = "requested"
        assert await browser.start_research(raced) is True
        assert raced.research_start_state == "confirmed"
        await set_document(page, turn(plan, extra='<dialog open>Confirm browser settings</dialog>'))
        blocked = receipt(page, expected=True)
        blocked.research_start_state = "requested"
        with pytest.raises(BrowserTaskError, match="blocked"):
            await browser.start_research(blocked)
        # A later human question creates a hard action boundary.
        await set_document(page, turn(plan, extra='<div data-message-author-role="user" data-message-id="user-later">Human question</div>'))
        boundary = receipt(page, expected=True)
        await browser.inspect(boundary)
        boundary.research_start_state = "requested"
        with pytest.raises(BrowserTaskError, match="latest user"):
            await browser.start_research(boundary)
    run_dom(tmp_path, turn(""), check)


def test_cancel_targets_only_matched_research_frame_and_confirms_stopped(tmp_path):
    async def check(browser, page):
        old_content = '<button onclick="parent.oldStops++">Stop research</button><div role="status">Researching 8 sources</div>'
        current_content = ("<button onclick=\"parent.currentStops++; document.body.innerHTML='<div role=status>Research stopped</div>'\">Stop research</button>"
                           '<div role="status">Researching 4 sources</div>')
        old = '<div data-message-author-role="assistant" data-message-id="assistant-old">' + frame(old_content) + '</div>'
        await set_document(page, turn(ACK + frame(current_content), prefix=old) +
                           '<script>window.currentStops=0; window.oldStops=0;</script>')
        current = receipt(page)
        assert await browser.request_stop(current) is True
        assert await page.evaluate("window.currentStops") == 1
        assert await page.evaluate("window.oldStops") == 0
        snapshot = await browser.inspect(current)
        assert snapshot.research_state == "stopped" and snapshot.generation_active is False
        # Unknown ownership cannot justify a click or a claim of stopped work.
        await set_document(page, turn(ACK, extra=frame(current_content)) +
                           '<script>window.currentStops=0;</script>')
        browser.settings = replace(browser.settings, browser_timeout_seconds=.35)
        assert await browser.request_stop(receipt(page)) is False
        assert await page.evaluate("window.currentStops") == 0
    run_dom(tmp_path, turn(""), check)


def test_ordinary_answers_clarifications_and_manual_blockers_are_preserved(tmp_path):
    async def check(browser, page):
        await set_document(page, turn("A normal answer with no research workflow."))
        plain = await browser.inspect(receipt(page))
        assert plain.state == "COMPLETE" and plain.research_observed is False
        old_panel = ('<section data-research-panel data-owner-message-id="assistant-old">'
                     '<div role="status">Researching 8 sources</div><button>Stop research</button></section>')
        await set_document(page, turn("A normal answer with no research workflow.", extra=old_panel))
        isolated = await browser.inspect(receipt(page))
        assert isolated.state == "COMPLETE" and isolated.research_observed is False
        await set_document(page, turn("Please clarify the date range."))
        clarification = await browser.inspect(receipt(page, expected=True))
        assert clarification.state == "NEEDS_USER_INPUT"
        assert clarification.user_input_kind == "content_clarification"
        await set_document(page, turn(ACK + frame(REPORT), extra='<dialog open>Please log in to ChatGPT.</dialog>'))
        blocked = await browser.inspect(receipt(page))
        assert blocked.state == "NEEDS_USER_INPUT" and blocked.user_input_kind == "manual_browser"
        assert blocked.result_text is None
    run_dom(tmp_path, turn(""), check)
