import pytest

from chatgpt_oracle.browser import SubmitReceipt, _prompt_text_sha256
from test_browser_controls import run_page
from test_browser_markdown_turns import USER_ID
from test_chatgpt_research_dom import ACK, frame, receipt, set_document, turn
from test_oracle_browser_boundaries import run_dom


PROMPT = '【CODEX_ORACLE】\n\nKeep **bold**, `a  b`, and the literal words 深度研究.'
ROUTING = '[$app](app://connector_openai_deep_research)'
CHIP = '<span data-prompt-link-href="app://connector_openai_deep_research" data-prompt-link-label="$app">深度研究</span>'


@pytest.mark.parametrize("native,confirmed", [(CHIP, True), ("", False), ("<code>" + CHIP + "</code>", False), (CHIP + CHIP, False)])
def test_routing_suffix_requires_this_messages_single_native_chip(tmp_path, native, confirmed):
    body = f'''<main><div data-chatgpt-search-message-ids="{USER_ID}">
      <div id="user" data-chatgpt-search-unit-key="fallback-turn-0:0:user">
        <div data-user-message-bubble>Keep <strong>bold</strong>, <code>a b</code>, and literal words 深度研究.{native}</div>
      </div></div><div data-message-author-role="assistant" data-message-id="reply">Finished normal answer</div><textarea></textarea></main>'''
    async def check(browser, page):
        await page.evaluate("value => document.querySelector('#user').__reactFiber$test={memoizedProps:{item:value}}", {
            "type": "user-message", "message": PROMPT + ROUTING, "messageId": USER_ID,
        })
        current = SubmitReceipt(page.url, 0, submission_state="unconfirmed", submitted_prompt_sha256=_prompt_text_sha256(PROMPT))
        snapshot = await browser.inspect(current)
        assert (current.submission_state == "confirmed") is confirmed
        assert current.submitted_prompt_sha256 == _prompt_text_sha256(PROMPT)
        if confirmed:
            assert snapshot.result_text == "Finished normal answer"
        else:
            assert snapshot.result_text is None
    run_page(tmp_path, body, check)


def native_report(completed=True):
    summary = "研究完成情况：3m · 2 次引用 · 2 个搜索" if completed else "正在研究…"
    return ('<main><div><div><div class="text-token-text-secondary">' + summary + '</div>'
            '<button aria-label="导出"></button><button aria-label="展开"></button>'
            '<div role="button"><div class="_reportPage_fixture_1"><h1>Native report</h1>'
            '<p>First page evidence. <a href="https://first.example/">One</a></p></div>'
            '<div class="_reportPage_fixture_2"><p>Second page evidence. <a href="https://second.example/">Two</a></p></div>'
            '</div></div></div></main>')


def test_native_turn_sibling_and_document_written_inner_frame(tmp_path):
    async def check(browser, page):
        # This reproduces the observed native sibling layout. The iframe is
        # outside the canonical assistant prose but inside one exact turn.
        for done in (False, True):
            body = ('<div data-content-search-turn-key="fallback-turn-0">' +
                    turn(ACK, extra=frame('<iframe id="root"></iframe>')) + '</div>')
            await set_document(page, body)
            outer = page.frames[1]
            await outer.evaluate("html => {const doc=document.querySelector('#root').contentDocument;doc.open();doc.write(html);doc.close();}", native_report(done))
            current = receipt(page)
            snapshot = await browser.inspect(current)
            if done:
                assert snapshot.state == "COMPLETE" and snapshot.research_report_complete
                assert "First page evidence" in snapshot.result_text and "Second page evidence" in snapshot.result_text
                assert "研究完成情况" not in snapshot.result_text
                assert snapshot.sources == ["https://first.example/", "https://second.example/"]
            else:
                assert snapshot.state == "RUNNING" and snapshot.result_text is None
        # Moving the same app beside another user's turn cannot prove ownership.
        await page.locator('[data-message-author-role="user"]').evaluate("n=>n.setAttribute('data-message-id','user-foreign')")
        unrelated = await browser.inspect(current)
        assert unrelated.result_text is None
    run_dom(tmp_path, turn(""), check)


def test_native_inner_plan_start_and_stop_are_one_matched_action(tmp_path):
    async def check(browser, page):
        body = '<div data-content-search-turn-key="fallback-turn-0">' + turn(ACK, extra=frame('<iframe id="root"></iframe>')) + '</div>'
        await set_document(page, body)
        outer = page.frames[1]
        await outer.evaluate(r"""() => {
          const doc=document.querySelector('#root').contentDocument;
          doc.open();doc.write('<section data-research-plan><button id="start">Start research</button></section>');doc.close();
          doc.querySelector('#start').onclick=()=>{
            doc.defaultView.startCount=(doc.defaultView.startCount||0)+1;
            doc.body.innerHTML='<div role="status">Researching 4 sources</div><button id="stop">Stop research</button>';
            doc.querySelector('#stop').onclick=()=>{
              doc.defaultView.stopCount=(doc.defaultView.stopCount||0)+1;
              doc.body.innerHTML='<div role="status">Research stopped</div>';
            };
          };
        }""")
        current = receipt(page, expected=True)
        current.research_start_state = "requested"
        assert await browser.start_research(current) is True
        assert current.research_start_state == "confirmed"
        assert await browser.request_stop(current) is True
        counts = await outer.evaluate("() => {const w=document.querySelector('#root').contentWindow;return [w.startCount,w.stopCount]}")
        assert counts == [1, 1]
        assert await page.locator('[data-message-author-role="user"]').count() == 1
    run_dom(tmp_path, turn(""), check)


def test_native_acknowledgement_waits_for_late_app_without_claiming_observation(tmp_path):
    async def check(browser, page):
        await set_document(page, turn("Deep Research 已启动，研究任务已提交。"))
        current = receipt(page)
        pending = await browser.inspect(current)
        assert pending.state == "RUNNING" and pending.result_text is None
        assert current.research_expected and not pending.research_observed
        assert pending.generation_active is False
        await set_document(page, turn('<pre>Deep Research 已启动，研究任务已提交。</pre><p>这是文档中的示例，不是运行状态。</p>'))
        ordinary = await browser.inspect(receipt(page))
        assert ordinary.state == "COMPLETE" and not ordinary.research_observed
    run_dom(tmp_path, turn(""), check)
