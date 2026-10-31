"""Small, private submission breadcrumbs; never store prompt or account DOM."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .models import utc_now

_current: ContextVar[dict | None] = ContextVar("submission_diagnostics", default=None)
logger = logging.getLogger(__name__)


@contextmanager
def submission_diagnostics(jobs_dir: Path, task_id: str, provider: str):
    path = jobs_dir / "diagnostics" / f"{task_id}.json"
    data = {"task_id": task_id, "provider": provider, "send_attempted": False, "events": []}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(previous, dict) and previous.get("task_id") == task_id
                and previous.get("provider") == provider and isinstance(previous.get("events"), list)
                and isinstance(previous.get("send_attempted"), bool)):
            data = previous
    except (OSError, ValueError):
        pass
    token = _current.set({"path": path, "data": data})
    try:
        yield
    finally:
        _current.reset(token)


def submission_identity() -> dict:
    current = _current.get()
    return ({key: current["data"][key] for key in ("task_id", "provider")}
            if current else {})


def submission_send_attempted() -> bool:
    """An earlier dispatch remains a one-way barrier across task restarts."""
    current = _current.get()
    return bool(current and current["data"].get("send_attempted"))


def submission_phase(phase: str, **details) -> bool:
    current = _current.get()
    if current is None:
        return True
    data, path = current["data"], current["path"]
    if phase == "send_attempted":
        data["send_attempted"] = True
    data["phase"] = phase
    event = {"phase": phase, "at": utc_now(), **details}
    data["events"] = (data.get("events", []) + [event])[-48:]
    if details.get("preparation_id"):
        previous = data.get("preparation", {})
        if previous.get("preparation_id") != details["preparation_id"]:
            previous = {}
        data["preparation"] = {**previous, **event, "send_attempted": data["send_attempted"]}
    # Repeated pending observations must not evict the evidence of the actual
    # send attempt or the UI immediately before it. Keep this bounded too.
    if phase in {"preparation_started", "draft_claimed", "draft_filled", "research_tool_prepared",
                 "draft_prepared", "draft_cleanup_finished", "preparation_failed",
                 "receipt_prepared", "gemini_before_send", "send_attempted", "send_confirmed",
                 "send_confirmed_late", "gemini_send_ack_pending", "gemini_send_ack_unavailable", "generation_wait_timeout",
                 "turn_stream_stage", "turn_stream_unsettled"}:
        data.setdefault("milestones", {})[phase] = event
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, path)
        temporary = None
        return True
    except OSError:
        logger.warning("Submission diagnostics unavailable for %s", data["task_id"])
        return False
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


# Process-level page probe. Task-local events above only exist while a job is
# active; a page can fail ("无法加载此 ChatGPT 对话", aborted startup requests,
# 403 backend calls) after that. Mirror a bounded, scrubbed ring per browser
# target so the failure survives turn cleanup and can be inspected later.
_probes: dict[str, dict] = {}
_MAX_PROBE_EVENTS = 60


def _probe_scrub(url: str) -> str:
    import re as _re
    return _scrub_value(str(url or ""))[:200]


def _scrub_value(text: str) -> str:
    """Remove hosts and credential-like query values from one probe string."""
    import re as _re
    text = _re.sub(r"^https?://[^/]+", "", text)
    # Any query parameter whose name looks secret is redacted everywhere,
    # including inside console messages that quote request URLs.
    text = _re.sub(r"([?&][^=&\s\"']*(?:verify|token|sig|key|secret)[^=&\s\"']*=)[^&\s\"']*",
                   r"\1…", text, flags=_re.I)
    return text[:300]


def probe_event(target_id: str | None, kind: str, **details: object) -> None:
    """Record one bounded page event; never raises into browser code."""
    if not target_id:
        return
    try:
        ring = _probes.setdefault(str(target_id), {"events": []})
        event = {"at": utc_now(), "kind": kind}
        for key, value in details.items():
            if value is None:
                continue
            if key in {"url", "page_url"}:
                value = _probe_scrub(value)
            elif key == "text" and isinstance(value, str):
                value = _scrub_value(value)
            event[key] = str(value)[:300] if isinstance(value, str) else value
        ring["events"] = (ring["events"] + [event])[-_MAX_PROBE_EVENTS:]
    except Exception:
        pass


def probe_snapshot(target_id: str | None) -> list[dict]:
    return list(_probes.get(str(target_id), {"events": []})["events"]) if target_id else []


def console_probe_script(target_id: str | None) -> str:
    """Init script installed in the page's main world before page scripts run.

    It records console errors/warnings and failed backend requests into a
    ring on window; the driver reads it and folds it into probe_event().
    """
    return """
(() => {
  const key = '__oracleConsoleProbe';
  if (window[key]) return;
  const state = {errors: [], requests: []};
  Object.defineProperty(window, key, {value: state, configurable: false, enumerable: false});
  const record = (list, item) => { list.push(item); if (list.length > 40) list.shift(); };
  const now = () => Math.round(performance.now());
  const origError = console.error.bind(console), origWarn = console.warn.bind(console);
  console.error = (...args) => { try { record(state.errors, {t: now(), level: 'error',
      text: args.map(a => { try { return typeof a === 'string' ? a : (a && a.message) || String(a); }
      catch (e) { return '[unprintable]'; } }).join(' ').slice(0, 300)}); } catch (e) {} return origError(...args); };
  console.warn = (...args) => { try { record(state.errors, {t: now(), level: 'warn',
      text: args.map(a => typeof a === 'string' ? a : String(a && a.message || a)).join(' ').slice(0, 300)}); } catch (e) {} return origWarn(...args); };
  const origFetch = window.fetch;
  if (origFetch) window.fetch = function (...args) {
    try { const input = args[0]; const url = typeof input === 'string' ? input : input?.url || '';
      const done = (status, extra) => record(state.requests, {t: now(), status, url: String(url).slice(0, 200),
        ...(extra || {})});
      return origFetch.apply(this, args).then(r => { done(r.status, {opaque: r.type});
        return r; }, e => { done('fetch_error', {error: String(e).slice(0, 120)}); throw e; });
    } catch (e) { return origFetch.apply(this, args); }
  };
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try { this.addEventListener('loadend', () => record(state.requests, {t: now(), status: this.status,
        url: String(url).slice(0, 200), xhr: true})); } catch (e) {}
    return origOpen.call(this, method, url, ...rest);
  };
})();
"""


def read_console_probe_script() -> str:
    return """
(() => { const s = window.__oracleConsoleProbe;
  return s ? {errors: s.errors.splice(0), requests: s.requests.splice(0)} : null; })()
"""
