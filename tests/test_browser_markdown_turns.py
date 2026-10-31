import pytest

from chatgpt_oracle.browser import SubmitReceipt, _prompt_text_sha256
from test_browser_controls import run_page


USER_ID = "e2a45d9d-4438-4568-8a78-293d06e64a44"
OTHER_ID = "444ec988-44ec-41a9-817c-d6cccc942336"
PROMPT = "【CODEX_ORACLE】\n\nPlease review `best_score` and **submit** against the screenshot."


@pytest.mark.parametrize("bound,type_name,confirmed", [
    (USER_ID, "user-message", True),
    (OTHER_ID, "user-message", False),
    (USER_ID, "assistant-message", False),
])
def test_markdown_turn_uses_only_dom_bound_original_text(tmp_path, bound, type_name, confirmed):
    body = f'''<main><div data-chatgpt-search-message-ids="{bound}">
      <div id="user" data-content-search-unit-key="fallback-turn-0:0:user">
        <div data-user-message-bubble>【CODEX_ORACLE】
          Please review <code>best_score</code> and <strong>submit</strong> against the screenshot.
          <button>显示更多</button>
        </div>
      </div></div>
      <div data-message-author-role="assistant" data-message-id="original-reply">Original review result.</div>
      <div data-message-author-role="user" data-message-id="manual-followup">A later manual question.</div>
      <div data-message-author-role="assistant" data-message-id="later-reply">Do not return this later result.</div>
      <textarea></textarea></main>'''

    async def check(browser, page):
        await page.evaluate("""value => {
          document.querySelector('#user').__reactFiber$fixture={memoizedProps:{},return:{memoizedProps:{item:value}}};
        }""", {"type": type_name, "message": PROMPT, "messageId": USER_ID, "serverMessageId": USER_ID})
        receipt = SubmitReceipt(page.url, 0, submitted_prompt_sha256=_prompt_text_sha256(PROMPT),
                                submission_state="unconfirmed")
        surface = await browser._read_turn_surface(page)
        user = next(m for m in surface["messages"] if m["role"] == "user")
        assert _prompt_text_sha256(user["text"]) != receipt.submitted_prompt_sha256
        snapshot = await browser.inspect(receipt)
        if confirmed:
            assert user["prompt_text"] == PROMPT
            assert snapshot.submission_state == "confirmed"
            assert snapshot.state == "COMPLETE"
            assert snapshot.result_text == "Original review result."
            assert snapshot.current_assistant_message_id == "original-reply"
        else:
            assert "prompt_text" not in user
            assert snapshot.submission_state == "unconfirmed"
            assert snapshot.result_text is None
    run_page(tmp_path, body, check)


def test_duplicate_markdown_prompts_remain_ambiguous(tmp_path):
    body = ''.join(f'''<div data-chatgpt-search-message-ids="{identity}">
        <div id="u{index}" data-content-search-unit-key="fallback-turn-{index}:0:user">review <code>value</code></div>
        </div><div data-message-author-role="assistant" data-message-id="a{index}">answer {index}</div>'''
        for index, identity in enumerate([USER_ID, OTHER_ID])) + '<textarea></textarea>'
    async def check(browser, page):
        for index, identity in enumerate([USER_ID, OTHER_ID]):
            await page.evaluate("""value => document.querySelector(value.selector).__reactFiber$fixture={memoizedProps:{item:value.item}}""",
                {"selector": f"#u{index}", "item": {"type": "user-message", "message": "review `value`", "messageId": identity}})
        receipt = SubmitReceipt(page.url, 0, submitted_prompt_sha256=_prompt_text_sha256("review `value`"),
                                submission_state="unconfirmed")
        snapshot = await browser.inspect(receipt)
        assert snapshot.submission_state == "unconfirmed"
        assert snapshot.result_text is None
    run_page(tmp_path, body, check)
