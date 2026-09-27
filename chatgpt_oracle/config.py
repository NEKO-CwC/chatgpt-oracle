from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _csv(name: str) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return ()
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values:
        raise ValueError(f"{name} must contain at least one host")
    return values


@dataclass(frozen=True)
class Settings:
    root: Path
    jobs_dir: Path
    browser_profile_dir: Path
    chrome_executable: Path | None
    browser_headless: bool
    browser_start_minimized: bool
    browser_timeout_seconds: float
    consult_poll_seconds: float
    critic_poll_seconds: float
    research_poll_seconds: float
    consult_timeout_seconds: float
    critic_timeout_seconds: float
    research_timeout_seconds: float
    host: str
    port: int
    transport: str
    mcp_path: str
    bearer_token: str | None
    allow_insecure_remote: bool
    allowed_hosts: tuple[str, ...] = _DEFAULT_ALLOWED_HOSTS
    upload_max_file_bytes: int = 25 * 1024 * 1024
    upload_max_total_bytes: int = 64 * 1024 * 1024
    upload_max_files: int = 8
    upload_ttl_seconds: float = 900.0
    export_max_count: int = 1000
    export_inline_max_bytes: int = 512 * 1024
    export_ttl_seconds: float = 900.0
    export_audit_retention_seconds: float = 90 * 24 * 60 * 60
    export_max_snapshot_bytes: int = 64 * 1024 * 1024
    file_store_dir: Path | None = None
    file_store_max_file_bytes: int = 512 * 1024 * 1024
    file_store_max_total_bytes: int = 2 * 1024 * 1024 * 1024
    file_store_max_transfers: int = 4
    file_store_max_chunk_bytes: int = 8 * 1024 * 1024
    file_store_io_chunk_bytes: int = 256 * 1024
    file_store_ttl_seconds: float = 2 * 60 * 60
    file_store_metadata_retention_seconds: float = 90 * 24 * 60 * 60
    public_base_url: str | None = None
    sessions_dir: Path | None = None
    session_ttl_seconds: float = 7200.0
    browser_cdp_url: str | None = "http://127.0.0.1:9222"
    browser_launch_cdp: bool = True

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        root = Path(os.getenv("CHATGPT_ORACLE_ROOT", str(root or Path.cwd()))).resolve()
        jobs_dir = Path(os.getenv("CHATGPT_ORACLE_JOBS_DIR", str(root / "runtime" / "jobs"))).resolve()
        profile = Path(
            os.getenv(
                "CHATGPT_PROFILE_DIR",
                str(root / "runtime" / "chatgpt-browser-profile"),
            )
        ).resolve()
        executable_value = os.getenv("CHATGPT_CHROME_EXECUTABLE", "").strip()
        executable = Path(executable_value).resolve() if executable_value else None
        file_store_value = os.getenv("CHATGPT_FILE_STORE_DIR", "").strip()
        public_base_value = os.getenv("CHATGPT_ORACLE_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public_base_value and not public_base_value.startswith(("http://", "https://")):
            raise ValueError("CHATGPT_ORACLE_PUBLIC_BASE_URL must use http:// or https://")
        transport = os.getenv("CHATGPT_ORACLE_TRANSPORT", "stdio").strip().lower()
        if transport not in {"stdio", "http", "streamable-http"}:
            raise ValueError("CHATGPT_ORACLE_TRANSPORT must be stdio or http")
        mcp_path = os.getenv("CHATGPT_ORACLE_MCP_PATH", "/mcp").strip() or "/mcp"
        if not mcp_path.startswith("/"):
            mcp_path = "/" + mcp_path
        if len(mcp_path) > 1:
            mcp_path = mcp_path.rstrip("/")
        allowed_hosts = tuple(dict.fromkeys((*_DEFAULT_ALLOWED_HOSTS, *_csv("CHATGPT_ORACLE_ALLOWED_HOSTS"))))
        return cls(
            root=root,
            jobs_dir=jobs_dir,
            browser_profile_dir=profile,
            chrome_executable=executable,
            browser_headless=_bool("CHATGPT_BROWSER_HEADLESS", False),
            browser_start_minimized=_bool("CHATGPT_BROWSER_START_MINIMIZED", True),
            browser_cdp_url=os.getenv("CHATGPT_BROWSER_CDP_URL", "http://127.0.0.1:9222").strip() or None,
            browser_launch_cdp=_bool("CHATGPT_BROWSER_LAUNCH_CDP", True),
            browser_timeout_seconds=_float("CHATGPT_BROWSER_TIMEOUT_SECONDS", 30.0),
            consult_poll_seconds=_float("CHATGPT_CONSULT_POLL_SECONDS", 30.0),
            critic_poll_seconds=_float("CHATGPT_CRITIC_POLL_SECONDS", 30.0),
            research_poll_seconds=_float("CHATGPT_RESEARCH_POLL_SECONDS", 180.0),
            consult_timeout_seconds=_float("CHATGPT_CONSULT_TIMEOUT_SECONDS", 1800.0),
            critic_timeout_seconds=_float("CHATGPT_CRITIC_TIMEOUT_SECONDS", 1800.0),
            research_timeout_seconds=_float("CHATGPT_RESEARCH_TIMEOUT_SECONDS", 7200.0),
            host=os.getenv("CHATGPT_ORACLE_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_int("CHATGPT_ORACLE_PORT", 8787),
            transport=transport,
            mcp_path=mcp_path,
            bearer_token=os.getenv("CHATGPT_ORACLE_BEARER_TOKEN") or None,
            allow_insecure_remote=_bool("CHATGPT_ORACLE_ALLOW_INSECURE_REMOTE", False),
            allowed_hosts=allowed_hosts,
            upload_max_file_bytes=_int("CHATGPT_UPLOAD_MAX_FILE_BYTES", 25 * 1024 * 1024),
            upload_max_total_bytes=_int("CHATGPT_UPLOAD_MAX_TOTAL_BYTES", 64 * 1024 * 1024),
            upload_max_files=_int("CHATGPT_UPLOAD_MAX_FILES", 8),
            upload_ttl_seconds=_float("CHATGPT_UPLOAD_TTL_SECONDS", 900.0),
            export_max_count=_int("CHATGPT_EXPORT_MAX_COUNT", 1000),
            export_inline_max_bytes=_int("CHATGPT_EXPORT_INLINE_MAX_BYTES", 512 * 1024),
            export_ttl_seconds=_float("CHATGPT_EXPORT_TTL_SECONDS", 900.0),
            export_audit_retention_seconds=_float(
                "CHATGPT_EXPORT_AUDIT_RETENTION_SECONDS", 90 * 24 * 60 * 60
            ),
            export_max_snapshot_bytes=_int("CHATGPT_EXPORT_MAX_SNAPSHOT_BYTES", 64 * 1024 * 1024),
            file_store_dir=Path(file_store_value).resolve() if file_store_value else None,
            file_store_max_file_bytes=_int("CHATGPT_FILE_STORE_MAX_FILE_BYTES", 512 * 1024 * 1024),
            file_store_max_total_bytes=_int("CHATGPT_FILE_STORE_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024),
            file_store_max_transfers=_int("CHATGPT_FILE_STORE_MAX_TRANSFERS", 4),
            file_store_max_chunk_bytes=_int("CHATGPT_FILE_STORE_MAX_CHUNK_BYTES", 8 * 1024 * 1024),
            file_store_io_chunk_bytes=_int("CHATGPT_FILE_STORE_IO_CHUNK_BYTES", 256 * 1024),
            file_store_ttl_seconds=_float("CHATGPT_FILE_STORE_TTL_SECONDS", 2 * 60 * 60),
            file_store_metadata_retention_seconds=_float(
                "CHATGPT_FILE_STORE_METADATA_RETENTION_SECONDS", 90 * 24 * 60 * 60
            ),
            public_base_url=public_base_value or None,
            sessions_dir=Path(os.getenv("CHATGPT_SESSIONS_DIR", str(root / "runtime" / "sessions"))).resolve(),
            session_ttl_seconds=_float("CHATGPT_SESSION_TTL_SECONDS", 7200.0),
        )

    def prepare_dirs(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.parent.mkdir(parents=True, exist_ok=True)
        if self.sessions_dir:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def poll_seconds(self, mode: str) -> float:
        return {
            "consult": self.consult_poll_seconds,
            "critic": self.critic_poll_seconds,
            "research": self.research_poll_seconds,
        }[mode]

    def timeout_seconds(self, mode: str) -> float:
        return {
            "consult": self.consult_timeout_seconds,
            "critic": self.critic_timeout_seconds,
            "research": self.research_timeout_seconds,
        }[mode]

    def validate_http_security(self) -> None:
        if self.transport not in {"http", "streamable-http"}:
            return
        loopback = self.host in {"127.0.0.1", "localhost", "::1"}
        if not loopback and not self.bearer_token and not self.allow_insecure_remote:
            raise ValueError(
                "Remote MCP requires CHATGPT_ORACLE_BEARER_TOKEN, or explicitly "
                "set CHATGPT_ORACLE_ALLOW_INSECURE_REMOTE=1 for a private test network"
            )


def default_chrome_executable() -> Path | None:
    candidates = []
    if sys.platform == "win32":
        roots = [
            os.getenv("ProgramW6432"),
            os.getenv("PROGRAMFILES"),
            os.getenv("PROGRAMFILES(X86)"),
            "C:/Program Files",
            "C:/Program Files (x86)",
            "D:/Program Files",
            "D:/Program Files (x86)",
        ]
        for root in roots:
            if root:
                candidates.extend(
                    [
                        Path(root) / "Google/Chrome/Application/chrome.exe",
                        Path(root) / "Microsoft/Edge/Application/msedge.exe",
                    ]
                )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None
