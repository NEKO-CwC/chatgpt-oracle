from __future__ import annotations

import json

from chatgpt_oracle import submission_diagnostics as diagnostics


def test_probe_events_are_bounded_and_secret_scrubbed():
    target = "probe-target-scrub"
    long_url = "https://ws.chatgpt.com/p13/ws/user/user-abc?verify=SECRET-VERIFY-VALUE&x=1"
    for index in range(diagnostics._MAX_PROBE_EVENTS + 10):
        diagnostics.probe_event(target, "page_console_error", text=f"boom {index} https://chatgpt.com/x?access_token=SECRET-TOKEN")
    diagnostics.probe_event(target, "page_request_failed", status=502, url=long_url)
    events = diagnostics.probe_snapshot(target)
    assert len(events) == diagnostics._MAX_PROBE_EVENTS
    blob = json.dumps(events)
    assert "SECRET-VERIFY-VALUE" not in blob
    assert "SECRET-TOKEN" not in blob
    assert "ws.chatgpt.com" not in blob
    assert events[-1]["url"].startswith("/p13/ws/user/user-abc?verify=…")


def test_probe_script_install_and_drain_are_idempotent_strings():
    script = diagnostics.console_probe_script("t")
    assert "window.fetch" in script and "__oracleConsoleProbe" in script
    assert "splice(0)" in diagnostics.read_console_probe_script()


def test_probe_snapshot_of_unknown_target_is_empty():
    assert diagnostics.probe_snapshot("never-seen") == []
    assert diagnostics.probe_snapshot(None) == []
