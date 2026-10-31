from chatgpt_oracle.browser import SubmitReceipt
from test_chatgpt_research_dom import ACK, REPORT, frame, receipt, set_document, turn
from test_oracle_browser_boundaries import run_dom
from test_browser_turns import inspect, message, receipt as turn_receipt


def test_generated_report_words_are_not_workflow_status(tmp_path):
    async def check(browser, page):
        # Content may discuss these words without being an activity indicator.
        report = ('<article data-research-report data-state="complete">'
                  '<h1>Researching workflow terminology</h1><p>Researching</p>'
                  '<p>Research stopped</p><p>These are terms quoted by the completed report.</p></article>')
        await set_document(page, turn(ACK + frame(report)))
        snapshot = await browser.inspect(receipt(page))
        assert snapshot.state == "COMPLETE"
        assert snapshot.generation_active is False
        assert snapshot.research_report_complete
        assert "terms quoted" in snapshot.result_text
        # A model-generated title is not evidence that the research tool ended.
        pending = '<h1>Completed diagnostic report</h1><p>Draft prose is already visible.</p>'
        await set_document(page, turn(ACK + frame(pending)))
        snapshot = await browser.inspect(receipt(page))
        assert snapshot.state == "RUNNING"
        assert snapshot.generation_active is False
        assert not snapshot.research_report_complete and snapshot.result_text is None
    run_dom(tmp_path, turn(""), check)


def test_quiet_pending_research_does_not_invent_activity(tmp_path):
    snapshot = inspect(tmp_path, turn_receipt(confirmed=True, research_expected=True), [
        message("user-current", "user", "current question"),
        message("assistant-current", "assistant", "I have started the research."),
    ])
    assert snapshot.state == "RUNNING"
    assert snapshot.generation_active is False
    assert snapshot.result_text is None
