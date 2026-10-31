"""Read only Gemini's conversation surface, without account/storage inspection."""

READ_CONTROLLED_MENU = r"""trigger => {
  const visible=e=>!!e && !!e.getClientRects().length;
  const controls=trigger.getAttribute('aria-controls');
  const root=controls && document.getElementById(controls);
  return {
    controls, expanded:trigger.getAttribute('aria-expanded')==='true',
    visible:visible(trigger), disabled:trigger.disabled || trigger.getAttribute('aria-disabled')==='true',
    menu_visible:visible(root),
    items:root ? Array.from(root.querySelectorAll('[role="menuitem"]'))
      .filter(e=>visible(e) && e.closest('[role="menu"]')===root)
      .map(e=>(e.innerText||'').trim()) : []
  };
}"""

READ_SELECTION_DIAGNOSTICS = r"""() => ({
  path:location.pathname,
  picker:Array.from(document.querySelectorAll('button[data-test-id="bard-mode-menu-button"]')).map(e=>({
    label:e.getAttribute('aria-label'),controls:e.getAttribute('aria-controls'),expanded:e.getAttribute('aria-expanded'),disabled:e.disabled})),
  menus:Array.from(document.querySelectorAll('[role="menu"]')).filter(e=>e.getClientRects().length).map(e=>({
    id:e.id,items:Array.from(e.querySelectorAll('[role="menuitem"]'))
      .filter(i=>i.getClientRects().length && i.closest('[role="menu"]')===e)
      .map(i=>({label:(i.innerText||'').trim().slice(0,160),selected:i.classList.contains('selected'),disabled:i.getAttribute('aria-disabled')}))})),
  overlays:Array.from(document.querySelectorAll('.cdk-overlay-pane')).filter(e=>e.getClientRects().length)
    .map(e=>({discovery:!!e.querySelector('gem-discovery-tooltip'),dialog:!!e.querySelector('[role="dialog"]')})),
  composer_length:(document.querySelector('rich-textarea .ql-editor')?.innerText||'').trim().length
})"""

READ_SURFACE = r"""() => {
  const clean = s => (s || '').replace(/\u00a0/g, ' ').trim();
  const skip = e => e.matches('button,mat-icon,script,style,thinking-panel,.cdk-visually-hidden,.screen-reader-only');
  const render = node => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE || skip(node)) return '';
    const tag = node.tagName.toLowerCase();
    if (tag === 'code-block') {
      const pre = node.querySelector('pre');
      return pre ? render(pre) : clean(node.innerText);
    }
    if (tag === 'pre') {
      const code = node.querySelector('code') || node;
      const language = Array.from(code.classList).find(x => x.startsWith('language-'));
      return '\n\n```' + (language ? language.slice(9) : '') + '\n' +
        (code.textContent || '').trimEnd() + '\n```\n\n';
    }
    if (tag === 'table') {
      const rows = Array.from(node.querySelectorAll('tr')).map(row =>
        Array.from(row.querySelectorAll('th,td')).map(cell =>
          clean(cell.innerText).replace(/\|/g, '\\|').replace(/\n/g, '<br>')));
      if (!rows.length) return '';
      const width = Math.max(...rows.map(row => row.length));
      const line = row => '| ' + Array.from({length:width}, (_,i) => row[i] || '').join(' | ') + ' |';
      return '\n\n' + line(rows[0]) + '\n' + line(Array(width).fill('---')) + '\n' +
        rows.slice(1).map(line).join('\n') + '\n\n';
    }
    if (tag === 'img') return '[Image displayed in Gemini Web]';
    const body = Array.from(node.childNodes).map(render).join('');
    if (tag === 'br') return '\n';
    if (/^h[1-6]$/.test(tag)) return '\n\n' + '#'.repeat(Number(tag[1])) + ' ' + clean(body) + '\n\n';
    if (tag === 'p' || tag === 'div' && node.classList.contains('paragraph')) return '\n\n' + clean(body) + '\n\n';
    if (tag === 'li') return '\n- ' + clean(body);
    if (tag === 'ul' || tag === 'ol') return '\n' + body + '\n';
    if (tag === 'strong' || tag === 'b') return '**' + body + '**';
    if (tag === 'em' || tag === 'i') return '*' + body + '*';
    if (tag === 'code') return '`' + body + '`';
    if (tag === 'a') {
      const href = node.getAttribute('href') || '';
      return /^https?:\/\//.test(href) && !/googleusercontent\.com|blob:/.test(href)
        ? '[' + clean(body) + '](' + href + ')' : body;
    }
    return body;
  };
  const messages = [];
  for (const node of document.querySelectorAll('user-query,model-response')) {
    if (node.parentElement?.closest('user-query,model-response')) continue;
    if (node.tagName === 'USER-QUERY') {
      const textNode = (node.querySelector('.query-text') || node).cloneNode(true);
      textNode.querySelectorAll('h5,button,.cdk-visually-hidden').forEach(e => e.remove());
      messages.push({role:'user', text:clean(textNode.textContent),
        container_id:node.closest('.conversation-container[id]')?.id || null,
        dom_id:node.querySelector('[id^="user-query-content"]')?.id || null});
    } else {
      const content = node.querySelector('message-content');
      if (!content?.id) continue;
      const labels = Array.from(node.querySelectorAll('button')).filter(e => e.getClientRects().length).map(e =>
        e.getAttribute('aria-label') || clean(e.innerText));
      const sources = Array.from((content || node).querySelectorAll('a[href]')).map(e => e.href)
        .filter(h => /^https?:\/\//.test(h) && !/gemini\.google\.com|googleusercontent\.com/.test(h));
      messages.push({role:'assistant', id:content.id,
        container_id:node.closest('.conversation-container[id]')?.id || null,
        text:clean(render(content)).replace(/\n{3,}/g, '\n\n'), sources,
        complete:labels.some(s => /Good response|Bad response|赞此回答|不喜欢/i.test(s)),
        plan_ready:labels.some(s => /^(Start research|开始研究|启动研究)$/i.test(s)),
        has_images:!!content.querySelector('img'),
        has_research_card:!!node.querySelector('immersive-entry-chip'),
        research_processing:!!node.querySelector('gem-processing-card.processing')});
    }
  }
  const buttons = Array.from(document.querySelectorAll('button')).filter(e => e.getClientRects().length);
  const active = buttons.some(e => /^(Stop response|Stop research|停止回复|停止回答|停止研究)$/i.test(e.getAttribute('aria-label') || clean(e.innerText)));
  const alerts = Array.from(document.querySelectorAll('[role="alert"],gemini-quota-banner'))
    .filter(e => e.getClientRects().length && !e.closest('user-query,model-response')).map(e => clean(e.innerText));
  const panel = Array.from(document.querySelectorAll('deep-research-immersive-panel')).find(e => e.getClientRects().length);
  const body = panel && Array.from(panel.querySelectorAll('message-content,.markdown')).find(e => !e.closest('thinking-panel') && e.getClientRects().length);
  const research_report = body ? {text:clean(render(body)).replace(/\n{3,}/g, '\n\n'),
    sources:Array.from(body.querySelectorAll('a[href]')).map(e=>e.href).filter(h=>/^https?:\/\//.test(h) && !/googleusercontent\.com/.test(h)),
    busy:Array.from(panel.querySelectorAll('mat-progress-spinner,mat-spinner')).some(e=>e.getClientRects().length && !e.closest('thinking-panel'))} : null;
  return {messages, active, alerts, research_report};
}"""

READ_COMPOSER_ATTACHMENTS = r"""() => {
  const editor = document.querySelector('rich-textarea .ql-editor[contenteditable="true"]');
  const root = editor?.closest('input-container') || editor?.closest('text-input-field') || editor?.parentElement?.parentElement?.parentElement?.parentElement?.parentElement;
  if (!root) return {text:'', image_count:0, busy:false};
  const text = root.innerText || '';
  const removals = Array.from(root.querySelectorAll('button[aria-label]')).map(e => e.getAttribute('aria-label'))
    .filter(s => /^(close |remove file|移除附件|删除附件)/i.test(s));
  return {text, image_count:root.querySelectorAll('img').length, removals,
    busy:!!root.querySelector('[role="progressbar"],mat-progress-spinner,mat-spinner')};
}"""

# No prompt, filenames, account data or credentials enter these breadcrumbs.
READ_SUBMISSION_STATE = r"""() => {
  const visible=e=>!!e.getClientRects().length;
  const editor=document.querySelector('rich-textarea .ql-editor[contenteditable="true"]');
  const root=editor?.closest('input-container') || editor?.closest('text-input-field');
  const buttons=Array.from(document.querySelectorAll('button')).filter(visible);
  const send=buttons.find(e=>/^(Send message|发送消息)$/i.test(e.getAttribute('aria-label')||''));
  return {path:location.pathname, composer_length:(editor?.innerText||'').trim().length,
    attachment_images:root?.querySelectorAll('img').length || 0,
    attachments_busy:!!root?.querySelector('[role="progressbar"],mat-progress-spinner,mat-spinner'),
    send_visible:!!send, send_enabled:!!send && !send.disabled && send.getAttribute('aria-disabled')!=='true',
    stop_visible:buttons.some(e=>/^(Stop response|Stop research|停止回复|停止回答|停止研究)$/i.test(e.getAttribute('aria-label')||'')),
    user_nodes:document.querySelectorAll('user-query').length,
    assistant_nodes:document.querySelectorAll('model-response').length};
}"""
