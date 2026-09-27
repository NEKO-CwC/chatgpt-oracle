from pathlib import Path

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
