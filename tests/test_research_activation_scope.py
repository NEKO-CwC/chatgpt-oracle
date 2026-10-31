from chatgpt_oracle.browser import _prompt_text_sha256
import pytest
from test_research_drafts import PROMPT, URL, draft_fixture, prepare, prepare_browser
from test_oracle_browser_boundaries import run_dom


def test_research_continuation_never_clicks_a_historical_native_mention(tmp_path):
    body = draft_fixture() + '''<div data-message-author-role="user" data-message-id="old-user">
      <span data-prompt-link-href="app://connector_openai_deep_research" data-prompt-link-label="$app"
        onclick="window.oldMentionClicked=true;document.querySelector('#composer').remove()">深度研究</span>
      </div>'''
    async def check(browser, page):
        prepare_browser(browser, page)
        _, receipt = await prepare(browser, page, continuation=True)
        assert not await page.evaluate('window.oldMentionClicked || false')
        assert receipt.research_expected
        assert receipt.submitted_prompt_sha256 == _prompt_text_sha256(PROMPT)
        assert await page.locator('#composer [app-mention-path]').count() == 1
        assert await page.evaluate('window.sent') == 1
    run_dom(tmp_path, body, check, url=URL)


@pytest.mark.parametrize("floating", [False, True])
def test_modern_floating_primary_section_is_a_scoped_tool_menu(tmp_path, floating):
    body = draft_fixture().replace('data-mention-section-id="chatgpt-actions"',
                                  'data-mention-section-id="chatgpt-primary"' + (' data-composer-overlay-floating-ui="true"' if floating else ''))
    body = body.replace('id="research"', 'id="research" data-plugin-impression-id="connector_openai_deep_research"')
    async def check(browser, page):
        prepare_browser(browser, page)
        _, receipt = await prepare(browser, page)
        assert receipt.research_expected
        assert await page.locator('#composer [app-mention-path]').count() == 1
        assert await page.evaluate('window.sent') == 1
    run_dom(tmp_path, body, check, url="https://chatgpt.com/")
