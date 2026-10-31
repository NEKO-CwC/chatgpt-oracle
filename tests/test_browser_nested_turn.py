from chatgpt_oracle.browser import SubmitReceipt, _prompt_text_sha256
from test_oracle_browser_boundaries import run_dom


def test_nested_search_wrapper_and_canonical_message_are_one_answer(tmp_path):
    body='''<main><div data-content-search-unit-key="fallback-turn-0:0:user"><div>question</div></div>
    <div data-content-search-unit-key="fallback-turn-0:2:assistant">
      <div data-chatgpt-selection-message-id="canonical-answer">
        <div data-markdown-text-style="assistant-message">One answer with evidence.</div>
      </div>
    </div></main>'''
    async def check(browser,page):
        receipt=SubmitReceipt(page.url,0,submitted_prompt_sha256=_prompt_text_sha256('question'),submission_state='unconfirmed')
        snapshot=await browser.inspect(receipt)
        assert snapshot.state=='COMPLETE' and snapshot.result_text=='One answer with evidence.'
        assert snapshot.current_assistant_message_id=='canonical-answer'
        surface=await browser._read_turn_surface(page)
        assert len([m for m in surface['messages'] if m['role']=='assistant'])==1
        assert 'fallback-turn-0:2' in surface['messages'][-1]['container_ids']
    run_dom(tmp_path,body,check)


def test_two_distinct_messages_in_one_search_wrapper_are_preserved(tmp_path):
    body='''<main><div data-content-search-unit-key="fallback-turn-0:0:user">question</div>
    <div data-content-search-unit-key="group:assistant"><div data-chatgpt-selection-message-id="fragment-a"><div data-markdown-text-style="assistant-message">First fragment.</div></div>
      <div data-chatgpt-selection-message-id="fragment-b"><div data-markdown-text-style="assistant-message">Second fragment.</div></div>
    </div></main>'''
    async def check(browser,page):
        surface=await browser._read_turn_surface(page)
        ids={m['id'] for m in surface['messages']}
        assert {'fragment-a','fragment-b'}<=ids
        assert len([m for m in surface['messages'] if m['role']=='assistant'])==2
    run_dom(tmp_path,body,check)
