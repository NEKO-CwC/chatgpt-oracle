from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
WAIT_SCRIPT = ROOT / "scripts" / "wait-mcp-listen-address.ps1"
POWERSHELL = shutil.which("powershell.exe")


def run_powershell(program: str) -> subprocess.CompletedProcess:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is not available")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", program],
        capture_output=True,
        text=True,
        timeout=20,
    )


def ps_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def test_waits_for_assigned_address_before_allowing_broker_start(tmp_path):
    log = tmp_path / "logs" / "startup.log"
    result = run_powershell(f"""
$ErrorActionPreference = 'Stop'
$global:addressReads = 0
function Get-NetIPAddress {{
    [CmdletBinding()] param($IPAddress)
    $global:addressReads++
    if ($global:addressReads -eq 2) {{ [pscustomobject]@{{AddressState='Tentative'}} }}
    if ($global:addressReads -ge 3) {{ [pscustomobject]@{{AddressState='Preferred'}} }}
}}
function Start-Sleep {{ param($Milliseconds) }}
& {ps_literal(WAIT_SCRIPT)} -ListenHost '100.77.245.1' -TimeoutSeconds 5 -LogPath {ps_literal(log)}
if ($global:addressReads -ne 3) {{ throw 'Address readiness was not checked' }}
Write-Output 'BROKER_START_ALLOWED'
""")
    assert result.returncode == 0, result.stderr
    assert "BROKER_START_ALLOWED" in result.stdout
    saved = log.read_text(encoding="utf-8-sig")
    assert "Waiting up to" in saved
    assert "is ready" in saved


def test_missing_address_times_out_without_initializing_broker(tmp_path):
    log = tmp_path / "startup.log"
    result = run_powershell(f"""
$ErrorActionPreference = 'Stop'
function Get-NetIPAddress {{ [CmdletBinding()] param($IPAddress) }}
& {ps_literal(WAIT_SCRIPT)} -ListenHost '100.77.245.1' -TimeoutSeconds 0.05 -RetrySeconds 0.01 -LogPath {ps_literal(log)}
Write-Output 'BROKER_START_ALLOWED'
""")
    assert result.returncode != 0
    assert "BROKER_START_ALLOWED" not in result.stdout
    assert "broker was not initialized" in log.read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "0.0.0.0", "::", "localhost"])
def test_wildcard_loopback_and_hostname_do_not_wait_for_adapter(host):
    result = run_powershell(f"""
$ErrorActionPreference = 'Stop'
function Get-NetIPAddress {{ throw 'Adapter query should not be needed' }}
& {ps_literal(WAIT_SCRIPT)} -ListenHost {ps_literal(host)}
Write-Output 'BROKER_START_ALLOWED'
""")
    assert result.returncode == 0, result.stderr
    assert "BROKER_START_ALLOWED" in result.stdout


def test_hidden_broker_persists_native_bind_failure(tmp_path):
    # Use an unassigned documentation IP and isolated stores. No production
    # scheduler, browser, login, Session or request is accessed.
    env = {
        **os.environ,
        "ORACLE_PROVIDER": "chatgpt",
        "ORACLE_SHARED_BROWSER": "0",
        "CHATGPT_ORACLE_ROOT": str(tmp_path),
        "CHATGPT_ORACLE_JOBS_DIR": str(tmp_path / "jobs"),
        "CHATGPT_SESSIONS_DIR": str(tmp_path / "sessions"),
        "CHATGPT_FILE_STORE_DIR": str(tmp_path / "files"),
        "CHATGPT_ORACLE_TRANSPORT": "http",
        "CHATGPT_ORACLE_HOST": "192.0.2.99",
        "CHATGPT_ORACLE_PORT": "18787",
        "CHATGPT_ORACLE_BEARER_TOKEN": "isolated-startup-test",
        "CHATGPT_ORACLE_TAILSCALE_IPS": "",
    }
    env.pop("CHATGPT_ORACLE_TAILSCALE_IPS")
    result = subprocess.run(
        [sys.executable, "-m", "chatgpt_oracle.server"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 3, result.stderr
    saved = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "runtime" / "logs").glob("*.log"))
    assert "uvicorn.error" in saved
    assert "bind" in saved.lower()
    assert "192.0.2.99" in saved
    assert "isolated-startup-test" not in saved
