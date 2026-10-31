"""Conservative DOM evidence for ChatGPT's separate research workflow.

These probes deliberately do not treat an iframe's body text as a report.
Ownership is checked before the content probe runs, including for portals.
Unknown UI variants stay pending rather than yielding a progress message.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


RESEARCH_PANEL_SELECTOR = (
    '[data-research-panel], [data-testid="deep-research-panel"], '
    '[data-testid="research-panel"], [data-research-report], '
    '[role="dialog"][aria-label*="research" i], '
    '[role="complementary"][aria-label*="research" i], '
    '[role="dialog"][aria-label*="研究"], [role="complementary"][aria-label*="研究"]'
)


RESEARCH_ELEMENT_METADATA = r"""node => {
  const visible = Boolean(node && node.getClientRects().length);
  const ownerIds = [], runIds = [], panelIds = [];
  const add = (values, value) => { if (value && !values.includes(value)) values.push(value); };
  let canonicalOwner = null;
  for (let owner = node; owner; owner = owner.parentElement) {
    if (!canonicalOwner) {
      canonicalOwner = owner.getAttribute('data-message-id') || owner.getAttribute('data-chatgpt-selection-message-id') ||
        (owner.getAttribute('data-content-search-unit-key') || owner.getAttribute('data-chatgpt-search-unit-key') || '').replace(/:(user|assistant)$/, '') || null;
    }
    for (const name of ['data-owner-message-id', 'data-parent-message-id', 'data-user-message-id',
                        'data-assistant-message-id', 'data-research-message-id']) add(ownerIds, owner.getAttribute(name));
    for (const name of ['data-research-run-id', 'data-research-id', 'data-tool-call-id']) add(runIds, owner.getAttribute(name));
    add(panelIds, owner.id);
    if (owner.matches('[data-message-author-role], article[data-testid*="conversation-turn"]')) break;
  }
  if (canonicalOwner) add(ownerIds, canonicalOwner);
  // The native MCP app is a sibling of the assistant prose, inside the same
  // virtualized turn. Bind that narrow container's single user identity;
  // never search the whole conversation for a convenient matching message.
  const turn = node.closest('[data-content-search-turn-key], [data-turn-key]');
  const turnUserIds = turn ? [...new Set([...turn.querySelectorAll(
    '[data-message-author-role="user"], [data-chatgpt-search-unit-key$=":user"], [data-content-search-unit-key$=":user"]')]
    .map(element => element.getAttribute('data-message-id') || element.getAttribute('data-chatgpt-selection-message-id') ||
      (element.getAttribute('data-chatgpt-search-unit-key') || element.getAttribute('data-content-search-unit-key') || '').replace(/:user$/, ''))
    .filter(Boolean))] : [];
  return {visible, owner_ids: ownerIds, canonical_owner: canonicalOwner, run_ids: runIds, panel_ids: panelIds,
    turn_user_ids: turnUserIds,
    contains_research_frame: [...node.querySelectorAll('iframe')].some(frame => frame.getClientRects().length &&
      (/^(?:深度研究|Deep research)$/i.test(frame.getAttribute('title') || '') || /deep-research/i.test(frame.getAttribute('src') || ''))),
    title: node.getAttribute('title') || '', src: node.getAttribute('src') || ''};
}"""


RESEARCH_CONTENT_PROBE = r"""(root, options) => {
  const visible = node => Boolean(node && node.getClientRects().length);
  const label = node => (node.getAttribute('aria-label') || node.innerText || '').trim();
  const stopPattern = /^(?:Stop generating|停止生成|Stop|停止|停止研究|Stop research)$/i;
  const startPattern = /^(?:Start research|开始研究|启动研究|开始深度研究)$/i;
  const activePattern = /^(?:(?:Pro|Deep)\s*)?(?:Searching|Researching|Finalizing|Thinking|Research\s+(?:in progress|underway)|思考中|正在思考|正在搜索|正在研究|正在生成|研究进行中|研究中)(?:\b|\s|[.…:：\d]|$)/i;
  const activeLinePattern = /^(?:(?:(?:Pro|Deep)\s*)?(?:Searching|Researching|Finalizing|Thinking)(?:\s+\d+\s+(?:sources?|websites?|pages?))?|Research\s+(?:in progress|underway)|思考中|正在思考|正在搜索|正在研究|正在生成|研究进行中|研究中)[.…]*$/i;
  const completePattern = /^(?:Research (?:is )?(?:complete|completed|finished)|(?:Deep research|Report) (?:is )?(?:complete|completed|ready)|研究已完成|研究完成|报告已完成|研究报告已就绪)[.!。！\s]*$/i;
  const completedHeading = /^(?:(?:Completed|Finished|Final)\s+(?:[\w-]+\s+){0,3}report|(?:已完成的?|最终)研究报告|研究报告[（(]?已完成[）)]?)[.!。！\s]*$/i;
  const stoppedPattern = /^(?:Research (?:was |is )?(?:stopped|cancelled|canceled)|Stopped|Cancelled|Canceled|研究已停止|已停止研究|研究已取消|已取消研究)[.!。！\s]*$/i;
  const placeholderPattern = /^(?:Your report will appear here(?: when .*?)?\.?|Report (?:is )?(?:not ready|loading|pending).*|Loading|Preparing(?: (?:the |your )?report)?|Please wait|研究(?:结束|完成)后.*报告.*|报告(?:将在|将会|会在).*|正在(?:加载|准备)报告.*|报告加载中|请稍候|加载中)[.…]*$/i;
  const controls = [...root.querySelectorAll('button, [role="button"]')].filter(visible);
  const statuses = [...root.querySelectorAll('[role="status"], [role="progressbar"], [data-testid="research-status"]')].filter(visible);
  const lines = (root.innerText || '').split(/\n/).map(line => line.trim()).filter(Boolean);
  const hasStop = controls.some(node => stopPattern.test(label(node)));
  const incompleteProgress = statuses.some(node => {
    if (node.getAttribute('role') !== 'progressbar') return false;
    const now = node.getAttribute('aria-valuenow'), max = node.getAttribute('aria-valuemax');
    return now === null || !Number.isFinite(Number(now)) || Number(now) < (max === null ? 100 : Number(max));
  });
  const active = root.getAttribute('aria-busy') === 'true' || hasStop || incompleteProgress ||
    statuses.some(node => node.getAttribute('aria-busy') === 'true' || activePattern.test(label(node)));
  const planReady = controls.some(node => !node.disabled && node.getAttribute('aria-disabled') !== 'true' &&
    (startPattern.test(label(node)) || (/^(?:Start|开始)$/i.test(label(node)) &&
      (options?.allow_generic_start || node.closest('[data-research-plan], [data-testid="research-plan"]')))));
  const stopped = !active && statuses.some(node => stoppedPattern.test(label(node)));
  const completionEvidence = [];
  if (statuses.some(node => completePattern.test(label(node)))) completionEvidence.push('completed_status');
  let nativePages = [];
  // Observed Skybridge UI: a product completion summary and Export/Expand
  // controls precede the paginated document preview. This is independent of
  // any words or headings generated inside the report itself.
  for (const summary of root.querySelectorAll('main > div > div > div')) {
    if (!visible(summary) || !/^研究完成情况[：:]/.test((summary.innerText || '').trim()) ||
        summary.closest('[class*="_reportPage_"]')) continue;
    const widget = summary.parentElement;
    const buttons = [...widget.querySelectorAll('button')].filter(visible);
    if (!buttons.some(node => /^(?:导出|Export)$/i.test(label(node))) ||
        !buttons.some(node => /^(?:展开|Expand)$/i.test(label(node)))) continue;
    const pages = [...widget.querySelectorAll('[class*="_reportPage_"]')].filter(visible);
    if (pages.length && pages.some(page => page.querySelector('p, li, td, pre'))) {
      nativePages = pages;
      completionEvidence.push('native_completed_research_summary');
      break;
    }
  }
  const candidates = [...root.querySelectorAll(
    '[data-research-report], [data-testid="research-report"], [data-testid="deep-research-report"], ' +
    '[role="document"][aria-label*="report" i], [role="document"][aria-label*="报告"], article')].filter(visible);
  if (root.matches('[data-research-report], [data-testid="research-report"], [data-testid="deep-research-report"], article')) candidates.unshift(root);
  const finishedHeading = [...root.querySelectorAll('h1, h2')].find(node => visible(node) && completedHeading.test(label(node)));
  if (finishedHeading) {
    // Generated document titles identify candidates, not upstream completion.
    // A separate product status/attribute must confirm the report has finished.
    const parent = finishedHeading.closest('article, [data-research-report], main, section') || finishedHeading.parentElement;
    if (parent && !candidates.includes(parent)) candidates.push(parent);
  }
  const prose = node => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE || !visible(node) ||
        node.matches('button, [role="button"], [role="status"], [role="progressbar"], nav, header, footer, iframe, script, style, [data-research-plan], [data-research-placeholder]')) return '';
    if (node.tagName === 'BR') return '\n';
    return [...node.childNodes].map(prose).join('') + (/^(?:P|DIV|LI|H[1-6]|PRE|TR|SECTION|ARTICLE)$/.test(node.tagName) ? '\n' : '');
  };
  let report = null, reportText = '', reportEvidence = [];
  for (const candidate of candidates) {
    const evidence = [];
    for (const name of ['data-state', 'data-status', 'data-research-status', 'data-report-status']) {
      if (/^(?:complete|completed|finished|ready)$/i.test(candidate.getAttribute(name) || '')) evidence.push('completed_report_attribute');
    }
    const text = prose(candidate).replace(/\n{3,}/g, '\n\n').trim();
    if (!text || placeholderPattern.test(text) || ![...completionEvidence, ...evidence].length) continue;
    const hasBody = [...candidate.querySelectorAll('p, li, td, pre, [data-markdown-text-style]')].filter(node => visible(node) &&
      !node.closest('[role="status"], [role="progressbar"], [data-research-placeholder], [data-research-plan]')).some(node => {
        const body = prose(node).trim();
        return body && !placeholderPattern.test(body) && !activeLinePattern.test(body) && !completePattern.test(body) &&
          !/^(?:\d+(?:\.\d+)?\s*%|\d+\s+(?:sources?|websites?|pages?))$/i.test(body);
      });
    if (!hasBody) continue;
    if (!report || (candidate !== root && report === root) || text.length > reportText.length) {
      report = candidate; reportText = text; reportEvidence = evidence;
    }
  }
  if (nativePages.length) {
    const text = nativePages.map(prose).map(value => value.trim()).filter(Boolean).join('\n\n');
    if (text) { report = nativePages[0]; reportText = text; }
  }
  const complete = !active && !planReady && !stopped && Boolean(report && reportText && [...completionEvidence, ...reportEvidence].length);
  const sourceRoots = nativePages.length ? nativePages : report ? [report] : [];
  const sources = sourceRoots.flatMap(container => [...container.querySelectorAll('a[href]')].filter(node => {
    if (!visible(node)) return false;
    for (let parent = node.parentElement; parent && parent !== container; parent = parent.parentElement) {
      if (parent.matches('button, [role="button"], nav, header, footer, [role="status"], [data-research-plan]')) return false;
    }
    return true;
  }).map(node => node.href));
  const runOwner = root.closest('[data-research-run-id], [data-research-id], [data-tool-call-id]');
  const runId = runOwner && (runOwner.getAttribute('data-research-run-id') || runOwner.getAttribute('data-research-id') || runOwner.getAttribute('data-tool-call-id'));
  return {active, plan_ready: planReady, stopped, complete,
    text: complete ? reportText : '', sources: sources,
    completion_evidence: complete ? [...new Set([...completionEvidence, ...reportEvidence])] : [],
    progress: complete ? null : statuses.map(label).find(Boolean) || lines.find(line => line.length < 240 && (activeLinePattern.test(line) || placeholderPattern.test(line))) || null,
    run_id: runId || null, report_id: report?.getAttribute('data-report-id') || report?.id || null};
}"""


NESTED_RESEARCH_CONTENT_PROBE = r"""(root, options) => {
  const probe = (__CONTENT_PROBE__);
  const values = [], seen = new Set();
  const read = (node, depth) => {
    if (!node || depth > 6 || values.length >= 16 || seen.has(node)) return;
    seen.add(node);
    values.push(probe(node, options));
    for (const frame of node.querySelectorAll('iframe')) {
      if (!frame.getClientRects().length) continue;
      // Native Skybridge uses a same-origin document-written inner iframe
      // which is not always exposed as a Playwright Frame. Ordinary DOM
      // access is sufficient; cross-origin documents remain inaccessible.
      try { read(frame.contentDocument?.body, depth + 1); } catch (_) {}
    }
  };
  read(root, 0);
  return values;
}""".replace("__CONTENT_PROBE__", RESEARCH_CONTENT_PROBE)


NESTED_RESEARCH_CONTROL = r"""(root, options) => {
  const pattern = new RegExp(options.pattern, 'i'), found = [], seen = new Set();
  const read = (node, depth) => {
    if (!node || depth > 6 || seen.size >= 16 || seen.has(node)) return;
    seen.add(node);
    // The caller already handles controls in the exposed outer document.
    // This fallback reaches only ordinary same-origin inner documents.
    if (depth > 0) for (const control of node.querySelectorAll('button, [role="button"]')) {
      if (!control.getClientRects().length || control.disabled || control.getAttribute('aria-disabled') === 'true' ||
          control.closest('pre, code, blockquote, [class*="_reportPage_"]')) continue;
      if (pattern.test((control.getAttribute('aria-label') || control.innerText || '').trim())) found.push(control);
    }
    for (const frame of node.querySelectorAll('iframe')) {
      if (!frame.getClientRects().length) continue;
      try { read(frame.contentDocument?.body, depth + 1); } catch (_) {}
    }
  };
  read(root, 0);
  if (options.action === 'click' && found.length === 1) {
    found[0].click();
    return {count: 1, clicked: true};
  }
  return {count: found.length, clicked: false};
}"""


def research_association(metadata: dict[str, Any], *, message_ids: set[str], run_ids: set[str], panel_ids: set[str]) -> str:
    """Return matched/foreign/unknown using explicit DOM identity relations."""
    owners = set(metadata.get("owner_ids") or [])
    canonical = metadata.get("canonical_owner")
    if canonical and canonical not in message_ids:
        return "foreign"
    related_runs = set(metadata.get("run_ids") or [])
    if related_runs and run_ids and not related_runs & run_ids:
        return "foreign"
    if owners:
        return "matched" if owners <= message_ids and owners & message_ids else "foreign"
    turn_users = set(metadata.get("turn_user_ids") or [])
    if turn_users:
        return "matched" if len(turn_users) == 1 and turn_users <= message_ids else "foreign"
    if related_runs & run_ids or set(metadata.get("panel_ids") or []) & panel_ids:
        return "matched"
    return "unknown"


def empty_research_surface() -> dict[str, Any]:
    return {"text": "", "sources": [], "active": False, "detected": False, "matched": False,
            "unassociated": False, "complete": False, "plan_ready": False, "stopped": False,
            "state": None, "progress": None, "run_id": None, "report_id": None, "completion_evidence": []}


def merge_research_surfaces(values: list[dict[str, Any]], *, unassociated: bool = False) -> dict[str, Any]:
    """Keep terminal evidence separate from text and let active UI veto it."""
    result = empty_research_surface()
    result.update(detected=bool(values) or unassociated, matched=bool(values), unassociated=unassociated)
    result["active"] = any(value.get("active") for value in values)
    result["plan_ready"] = any(value.get("plan_ready") for value in values)
    result["stopped"] = bool(values) and any(value.get("stopped") for value in values) and not result["active"]
    reports = [value for value in values if value.get("complete") and value.get("completion_evidence") and value.get("text")]
    # Conflicting matched report identities are ambiguous, even if the text is
    # stable. A nested frame may repeat the same report with no explicit ID.
    report_ids = {value["report_id"] for value in reports if value.get("report_id")}
    run_ids = {value["run_id"] for value in values if value.get("run_id")}
    result["complete"] = bool(reports) and len(report_ids) <= 1 and len(run_ids) <= 1 and not (
        result["active"] or result["plan_ready"] or result["stopped"] or unassociated)
    if result["complete"]:
        report = max(reports, key=lambda value: len(value["text"]))
        result.update(text=report["text"], report_id=report.get("report_id"),
                      completion_evidence=list(report["completion_evidence"]))
    for value in values:
        if value.get("progress") and not result["progress"]:
            result["progress"] = value["progress"]
        if value.get("run_id") and len(run_ids) <= 1 and not result["run_id"]:
            result["run_id"] = value["run_id"]
        for href in value.get("sources", []):
            if not isinstance(href, str):
                continue
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https"} and parsed.hostname not in {"chatgpt.com", "chat.openai.com"} and href not in result["sources"]:
                result["sources"].append(href)
    result["sources"] = result["sources"][:30]
    if result["detected"]:
        result["state"] = ("unassociated" if unassociated else "running" if result["active"] else
                           "plan_ready" if result["plan_ready"] else "stopped" if result["stopped"] else
                           "complete" if result["complete"] else "report_pending")
    return result
