import pytest

from chatgpt_oracle.browser import BrowserComposerDraftPresent, BrowserPreparationError
from chatgpt_oracle.submission_diagnostics import submission_diagnostics
from test_oracle_browser_boundaries import run_dom


PROMPT = "Native editor proof\n    value = 'a  b'\nLast line"
BODY = '<main><div data-composer-body id="owner"><div id="editor" contenteditable="true" style="white-space:pre-wrap"></div></div></main>'
INSTALL = r"""initial => {
  const node=document.querySelector('#editor'), owner=document.querySelector('#owner');
  const documentNode=lines=>({
    lines, textContent:lines.join(''), content:{size:lines.join('\n').length+2},
    toJSON:()=>({type:'doc',content:lines.map(text=>({type:'paragraph',text}))}),
    descendants:visit=>{for(const text of lines)if(text)visit({isText:true,isLeaf:true,text,type:{name:'text'}})},
  });
  let model=documentNode(initial.split('\n'));
  const view={dom:node,state:{schema:{nodes:{paragraph:{create:(_attrs,content)=>({text:content?.text||''})}},text:text=>({text})},selection:{}},
    dispatch:transaction=>{
      model=documentNode(transaction.lines);window.persistedDraft=transaction.lines.join('\n');
      window.nativeDispatches++;
      node.replaceChildren(...transaction.lines.map(text=>{const p=document.createElement('p');p.textContent=text;return p}));
      node.pmViewDesc.node=model;
    }};
  Object.defineProperty(view.state,'doc',{get:()=>model});
  Object.defineProperty(view.state,'tr',{get:()=>({lines:[...model.lines],
    replaceWith(_from,_to,paragraphs){this.lines=paragraphs.map(p=>p.text);return this},
    delete(){this.lines=[''];return this},
  })});
  node.pmViewDesc={node:model};
  owner.__reactFiber$native={memoizedProps:{composerController:{view}}};
  node.textContent=initial;window.nativeDispatches=0;window.persistedDraft=initial;
}"""


def test_fill_and_cleanup_commit_the_native_model_and_persistence(tmp_path):
    async def check(browser, page):
        await page.evaluate(INSTALL, "")
        with submission_diagnostics(tmp_path / "jobs", "native-model", "chatgpt"):
            preparation = browser._new_composer_preparation(page, PROMPT, "consult")
            await browser._fill_prepared_draft(page, preparation)
            assert await page.evaluate('window.persistedDraft') == PROMPT
            assert await browser._cleanup_prepared_draft(page, preparation)
        assert await page.evaluate('window.persistedDraft') == ""
        assert await page.evaluate('window.nativeDispatches') == 2
        state = await browser._read_composer_draft(page.locator('#editor'))
        assert state["native_model_present"] and state["native_model_empty"]
        assert not state["text"].strip()
    run_dom(tmp_path, BODY, check)


def test_hidden_native_draft_is_protected_even_when_the_dom_looks_empty(tmp_path):
    async def check(browser, page):
        await page.evaluate(INSTALL, "Human native draft")
        await page.locator('#editor').evaluate('node=>node.replaceChildren()')
        with submission_diagnostics(tmp_path / "jobs", "hidden-native", "chatgpt"):
            preparation = browser._new_composer_preparation(page, PROMPT, "consult")
            with pytest.raises(BrowserComposerDraftPresent):
                await browser._fill_prepared_draft(page, preparation)
        assert await page.evaluate('window.persistedDraft') == "Human native draft"
        assert await page.evaluate('window.nativeDispatches') == 0
    run_dom(tmp_path, BODY, check)


def test_unbound_native_editor_never_falls_back_to_dom_only_writes(tmp_path):
    async def check(browser, page):
        await page.evaluate(INSTALL, "")
        await page.evaluate("delete document.querySelector('#owner').__reactFiber$native")
        with submission_diagnostics(tmp_path / "jobs", "unbound-native", "chatgpt"):
            preparation = browser._new_composer_preparation(page, PROMPT, "consult")
            with pytest.raises(BrowserPreparationError):
                await browser._fill_prepared_draft(page, preparation)
        assert await page.evaluate('window.nativeDispatches') == 0
        assert not (await page.locator('#editor').inner_text()).strip()
    run_dom(tmp_path, BODY, check)


def test_owned_native_rewrite_is_a_service_error_and_can_be_cleaned(tmp_path):
    async def check(browser, page):
        await page.evaluate(INSTALL, "")
        await page.evaluate("""() => {
          const view=document.querySelector('#owner').__reactFiber$native.memoizedProps.composerController.view;
          const dispatch=view.dispatch;
          view.dispatch=transaction=>{
            if(transaction.lines.some(Boolean))transaction.lines.push('unexpected editor transformation');
            dispatch(transaction);
          };
        }""")
        with submission_diagnostics(tmp_path / "jobs", "rewritten-native", "chatgpt"):
            preparation = browser._new_composer_preparation(page, PROMPT, "consult")
            with pytest.raises(BrowserPreparationError):
                await browser._fill_prepared_draft(page, preparation)
            assert preparation.filled
            assert await browser._cleanup_prepared_draft(page, preparation)
        assert await page.evaluate('window.persistedDraft') == ""
    run_dom(tmp_path, BODY, check)
