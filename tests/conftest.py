"""Keep local fake-browser regressions out of the running broker's stores."""
import os
import tempfile
from pathlib import Path

import pytest

# Server modules construct their managers during collection, before autouse
# fixtures run. Keep that initialization away from the running brokers too.
_collection_runtime = tempfile.TemporaryDirectory(prefix="oracle-pytest-collection-", ignore_cleanup_errors=True)
for _provider in ("CHATGPT", "GEMINI"):
    os.environ[f"{_provider}_ORACLE_ROOT"] = str(Path(_collection_runtime.name) / _provider.lower())
    for _suffix in ("ORACLE_JOBS_DIR", "SESSIONS_DIR", "FILE_STORE_DIR", "PROFILE_DIR", "ORACLE_BEARER_TOKEN"):
        os.environ.pop(f"{_provider}_{_suffix}", None)
os.environ["ORACLE_SHARED_BROWSER"] = "0"


@pytest.fixture(autouse=True)
def isolate_runtime_directories(monkeypatch, tmp_path):
    # Settings.from_env gives environment roots precedence. Tests already pass
    # temporary roots; inherited service paths must not redirect their stores.
    for name in (
        "CHATGPT_ORACLE_ROOT", "CHATGPT_ORACLE_JOBS_DIR", "CHATGPT_SESSIONS_DIR",
        "CHATGPT_FILE_STORE_DIR", "CHATGPT_PROFILE_DIR",
        "GEMINI_ORACLE_ROOT", "GEMINI_ORACLE_JOBS_DIR", "GEMINI_SESSIONS_DIR",
        "GEMINI_FILE_STORE_DIR", "GEMINI_PROFILE_DIR", "ORACLE_PROVIDER",
        "CHATGPT_ORACLE_TAILSCALE_IPS", "GEMINI_ORACLE_TAILSCALE_IPS",
        "CHATGPT_ORACLE_BEARER_TOKEN", "GEMINI_ORACLE_BEARER_TOKEN",
        "ORACLE_SHARED_BROWSER", "ORACLE_BROWSER_PROFILE_DIR", "ORACLE_BROWSER_CDP_URL", "ORACLE_BROWSER_LAUNCH_CDP",
    ):
        monkeypatch.delenv(name, raising=False)
    # Existing isolated-provider regressions retain their fixture contract.
    # Shared-pool tests explicitly opt in with temporary profile paths.
    monkeypatch.setenv("ORACLE_SHARED_BROWSER", "0")
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path / "shared-browser-profile"))
    monkeypatch.setenv("ORACLE_BROWSER_CDP_URL", "http://127.0.0.1:9333")
