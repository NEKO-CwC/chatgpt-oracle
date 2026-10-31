from pathlib import Path
from dataclasses import replace

import pytest

from chatgpt_oracle.config import Settings


def test_settings_keep_local_host_allowlist_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHATGPT_ORACLE_ROOT", str(tmp_path))
    monkeypatch.delenv("CHATGPT_ORACLE_ALLOWED_HOSTS", raising=False)

    settings = Settings.from_env()

    assert settings.allowed_hosts == ("127.0.0.1:*", "localhost:*", "[::1]:*")


def test_settings_add_explicit_public_hosts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHATGPT_ORACLE_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CHATGPT_ORACLE_ALLOWED_HOSTS",
        " neko-dashboard.com:32751, 192.0.2.10:8787, neko-dashboard.com:32751 ",
    )

    settings = Settings.from_env()

    assert settings.allowed_hosts == (
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "neko-dashboard.com:32751",
        "192.0.2.10:8787",
    )


def test_export_audit_retention_is_configurable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHATGPT_ORACLE_ROOT", str(tmp_path))
    monkeypatch.setenv("CHATGPT_EXPORT_AUDIT_RETENTION_SECONDS", "3600")

    assert Settings.from_env().export_audit_retention_seconds == 3600


def test_file_store_limits_and_ttl_are_configurable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHATGPT_ORACLE_ROOT", str(tmp_path))
    monkeypatch.setenv("CHATGPT_FILE_STORE_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("CHATGPT_FILE_STORE_MAX_FILE_BYTES", "104857600")
    monkeypatch.setenv("CHATGPT_FILE_STORE_MAX_TOTAL_BYTES", "524288000")
    monkeypatch.setenv("CHATGPT_FILE_STORE_MAX_TRANSFERS", "2")
    monkeypatch.setenv("CHATGPT_FILE_STORE_TTL_SECONDS", "3600")

    settings = Settings.from_env()

    assert settings.file_store_dir == (tmp_path / "staging").resolve()
    assert settings.file_store_max_file_bytes == 104857600
    assert settings.file_store_max_total_bytes == 524288000
    assert settings.file_store_max_transfers == 2
    assert settings.file_store_ttl_seconds == 3600


@pytest.mark.parametrize("provider,port", [("chatgpt", 8787), ("gemini", 8788)])
def test_tailscale_auth_and_host_settings_are_provider_specific(monkeypatch, tmp_path, provider, port):
    prefix = provider.upper()
    monkeypatch.setenv(f"{prefix}_ORACLE_TAILSCALE_IPS", "100.77.245.1,fd7a:115c:a1e0::4501:f503,100.77.245.1")
    monkeypatch.setenv(f"{prefix}_ORACLE_PORT", str(port))
    settings = Settings.from_env(tmp_path, provider=provider)
    assert settings.tailscale_ips == ("100.77.245.1", "fd7a:115c:a1e0::4501:f503")
    assert f"100.77.245.1:{port}" in settings.allowed_hosts
    assert f"[fd7a:115c:a1e0::4501:f503]:{port}" in settings.allowed_hosts
    other = "gemini" if provider == "chatgpt" else "chatgpt"
    assert Settings.from_env(tmp_path, provider=other).tailscale_ips == ()


@pytest.mark.parametrize("address", ["192.168.1.2", "127.0.0.1", "203.0.113.1", "100.64.0.0/10", "*", "fd00::1"])
def test_tailscale_auth_requires_exact_tailnet_addresses(monkeypatch, tmp_path, address):
    monkeypatch.setenv("CHATGPT_ORACLE_TAILSCALE_IPS", address)
    with pytest.raises(ValueError, match="exact Tailscale IP"):
        Settings.from_env(tmp_path)


def test_remote_without_bearer_requires_explicit_tailscale_access(tmp_path):
    settings = replace(Settings.from_env(tmp_path), transport="http", host="0.0.0.0",
                       bearer_token=None, allow_insecure_remote=False, tailscale_ips=())
    with pytest.raises(ValueError, match="requires"):
        settings.validate_http_security()
    replace(settings, tailscale_ips=("100.77.245.1",)).validate_http_security()
