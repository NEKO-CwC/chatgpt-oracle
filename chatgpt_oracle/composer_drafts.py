"""Task-local draft ownership, independent of request/turn text matching."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class ComposerPreparation:
    page: Any
    prompt: str
    mode: str
    preparation_id: str = field(default_factory=lambda: uuid4().hex)
    filled: bool = False
    uploads_started: bool = False
    signature: str | None = None
    tool_transition: bool = False
    rendered_text_sha256: str | None = None


# No page text is persisted. The signature and candidates stay inside this
# attempt; diagnostics receive digests only. A guard lives on this exact editor
# node, so a new tab, hydrated replacement, or inherited old draft cannot claim it.
COMPOSER_DRAFT_SCRIPT = r"""(node, options = {}) => {
    const researchPath = 'app://connector_openai_deep_research';
    const guards = window.__oracleComposerDraftGuards ||= new WeakMap();
    const boundView = value => value?.dom === node && value?.state?.doc && typeof value.dispatch === 'function';
    const findNativeView = () => {
        if (boundView(node.pmViewDesc?.view)) return node.pmViewDesc.view;
        // Observed native composer API, reached only through this editor's
        // own bounded React ancestry. Never inspect global application state.
        for (let owner = node, level = 0; owner && level < 8; owner = owner.parentElement, level++) {
            const key = Object.getOwnPropertyNames(owner).find(name => name.startsWith('__reactFiber$'));
            for (let fiber = key ? owner[key] : null, depth = 0; fiber && depth < 12; fiber = fiber.return, depth++) {
                const view = fiber.memoizedProps?.composerController?.view;
                if (boundView(view)) return view;
            }
        }
        return null;
    };
    const nativeView = findNativeView();
    const nativeDocument = () => nativeView?.state?.doc || node.pmViewDesc?.node || null;
    const nativeEmpty = () => {
        const doc = nativeDocument();
        if (!doc) return null;
        let material = false;
        doc.descendants(child => {
            if (child.isText && (child.text || '').trim()) material = true;
            if (child.isLeaf && !child.isText && !['hardBreak', 'hard_break'].includes(child.type?.name)) material = true;
        });
        return !material;
    };
    const signature = () => JSON.stringify({
        value: 'value' in node ? node.value : null, html: node.innerHTML,
        native_document: nativeDocument()?.toJSON?.() || null,
    });
    // The editor decorates some markdown with non-material widgets: a hex colour
    // written as inline code (for example `1px solid #E2E8F0`) gains an
    // aria-hidden, empty, non-editable colour swatch. That widget holds no
    // content, so it is not material the broker must preserve.
    // A node qualifies only as a contentless, app-marked decoration leaf: any
    // image/audio/canvas/frame stays user material however it is hidden, and
    // any non-editable node without aria-hidden stays protected.
    const mentionScope = e => e.closest('[app-mention-path="' + researchPath + '"]');
    const decorationOnly = e => e.tagName === 'SPAN'
        && e.getAttribute('contenteditable') === 'false'
        && e.getAttribute('aria-hidden') === 'true'
        && !(e.textContent || '').trim()
        && !e.querySelector('img,video,audio,canvas,svg,iframe,[contenteditable="false"]');
    const serialize = source => {
        if ('value' in source) return source.value;
        const copy = source.cloneNode(true);
        for (const mention of copy.querySelectorAll('[app-mention-path="' + researchPath + '"]')) mention.remove();
        for (const br of copy.querySelectorAll('br')) br.replaceWith('\n');
        for (const block of copy.querySelectorAll('p, div, li')) block.append('\n');
        return copy.textContent || '';
    };
    const pathTo = child => {
        const path = [];
        while (child && child !== node) {
            const parent = child.parentNode;
            if (!parent) return null;
            path.unshift([...parent.childNodes].indexOf(child)); child = parent;
        }
        return child === node ? path : null;
    };
    const atPath = (root, path) => {
        for (const index of path || []) root = root?.childNodes[index];
        return root;
    };
    const snapshot = () => {
        const mentions = [...node.querySelectorAll('[app-mention-path]')];
        const research = mentions.filter(m => m.getAttribute('app-mention-path') === researchPath);
        const candidates = [];
        // Only one newly activated tool and its directly adjacent U+0020 can
        // explain this transformation. Never strip spaces throughout a prompt.
        if (research.length === 1 && mentions.length === 1) {
            for (const [adjacent, edge] of [[research[0].previousSibling, 'end'], [research[0].nextSibling, 'start']]) {
                if (adjacent?.nodeType !== Node.TEXT_NODE) continue;
                const offset = edge === 'start' ? 0 : adjacent.data.length - 1;
                if (adjacent.data[offset] !== ' ') continue;
                const path = pathTo(adjacent), copy = node.cloneNode(true);
                atPath(copy, path).deleteData(offset, 1);
                candidates.push({path, edge, prompt_text: serialize(copy)});
            }
        }
        const guard = guards.get(node);
        return {
            text: 'value' in node ? node.value : (node.innerText || node.textContent || ''),
            prompt_text: serialize(node), signature: signature(),
            has_mention: mentions.length > 0, research_mentions: research.length,
            native_model_present: !!nativeDocument(), native_model_empty: nativeEmpty(),
            foreign_mentions: mentions.length - research.length,
            has_embedded_content: [...node.querySelectorAll('img,video,audio,canvas,svg,iframe,[contenteditable="false"]')]
                .some(embedded => !mentionScope(embedded) && !decorationOnly(embedded)),
            separator_candidates: candidates,
            preparation_id: guard?.id || null, interrupted: guard?.interrupted || false,
        };
    };
    const release = guard => {
        if (!guard) return;
        for (const type of guard.events) node.removeEventListener(type, guard.interrupt, true);
        guards.delete(node);
    };
    if (options.action === 'claim') {
        const initial = snapshot();
        if (initial.text.trim() || initial.has_mention || initial.has_embedded_content || initial.native_model_empty === false) return initial;
        release(guards.get(node));
        const guard = {id: options.preparation_id, interrupted: false,
            events: ['keydown', 'pointerdown', 'paste', 'drop', 'compositionstart']};
        // Broker filling/focusing and menu clicks do not type into or click
        // this editor. Human interaction invalidates cleanup authority even if
        // the eventual visible text happens to equal our prompt.
        guard.interrupt = event => { if (event.isTrusted) guard.interrupted = true; };
        for (const type of guard.events) node.addEventListener(type, guard.interrupt, true);
        guards.set(node, guard);
        return snapshot();
    }
    const guard = guards.get(node);
    const authorized = !!guard && guard.id === options.preparation_id && !guard.interrupted
        && options.signature === signature();
    if (options.action === 'fill') {
        if (!authorized) return {changed: false, ...snapshot()};
        node.focus();
        let changed = true;
        if ('value' in node) {
            const descriptor = Object.getOwnPropertyDescriptor(
                node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value');
            descriptor.set.call(node, options.text);
            node.dispatchEvent(new Event('input', {bubbles: true}));
        } else if (nativeView) {
            const schema = nativeView.state.schema;
            if (!schema?.nodes?.paragraph) return {changed: false, ...snapshot()};
            const paragraphs = options.text.split('\n').map(text => schema.nodes.paragraph.create(null, text ? schema.text(text) : null));
            let transaction = nativeView.state.tr.replaceWith(0, nativeView.state.doc.content.size, paragraphs);
            const selection = nativeView.state.selection?.constructor;
            if (typeof selection?.near === 'function') transaction = transaction.setSelection(selection.near(transaction.doc.resolve(transaction.doc.content.size), -1));
            nativeView.dispatch(transaction);
        } else if (node.pmViewDesc) {
            return {changed: false, ...snapshot()};
        } else {
            const selection = window.getSelection(), range = document.createRange();
            range.selectNodeContents(node); selection.removeAllRanges(); selection.addRange(range);
            changed = document.execCommand('insertText', false, options.text);
        }
        return {changed, ...snapshot()};
    }
    if (options.action === 'repair_separator') {
        if (!authorized) return {changed: false, ...snapshot()};
        const adjacent = atPath(node, options.path);
        const offset = options.edge === 'start' ? 0 : (adjacent?.data?.length || 0) - 1;
        if (adjacent?.nodeType !== Node.TEXT_NODE || adjacent.data[offset] !== ' ')
            return {changed: false, ...snapshot()};
        node.focus();
        if (nativeView) {
            const position = nativeView.posAtDOM(adjacent, offset);
            if (nativeView.state.doc.textBetween(position, position + 1, '\n', '\n') !== ' ')
                return {changed: false, ...snapshot()};
            nativeView.dispatch(nativeView.state.tr.delete(position, position + 1));
            return {changed: true, ...snapshot()};
        }
        if (node.pmViewDesc) return {changed: false, ...snapshot()};
        const selection = window.getSelection(), range = document.createRange();
        range.setStart(adjacent, offset); range.setEnd(adjacent, offset + 1);
        selection.removeAllRanges(); selection.addRange(range);
        // Use the editor's native edit route so ProseMirror observes the edit.
        const changed = document.execCommand('delete', false);
        return {changed, ...snapshot()};
    }
    if (options.action === 'clear') {
        if (!authorized) return {cleared: false, ...snapshot()};
        node.focus();
        if ('value' in node) {
            const descriptor = Object.getOwnPropertyDescriptor(
                node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value');
            descriptor.set.call(node, '');
            node.dispatchEvent(new Event('input', {bubbles: true}));
        } else if (nativeView) {
            nativeView.dispatch(nativeView.state.tr.delete(0, nativeView.state.doc.content.size));
        } else if (node.pmViewDesc) {
            return {cleared: false, ...snapshot()};
        } else {
            const selection = window.getSelection(), range = document.createRange();
            range.selectNodeContents(node); selection.removeAllRanges(); selection.addRange(range);
            document.execCommand('delete', false);
        }
        const result = snapshot();
        result.cleared = !result.text.trim() && !result.has_mention && !result.has_embedded_content && result.native_model_empty !== false;
        if (result.cleared) release(guard);
        return result;
    }
    if (options.action === 'release' && guard?.id === options.preparation_id) release(guard);
    return snapshot();
}"""
