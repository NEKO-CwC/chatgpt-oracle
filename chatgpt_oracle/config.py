from __future__ import annotations

import os
import ipaddress
import sys
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_SHARED_BROWSER_ENV = (
    "ORACLE_SHARED_BROWSER", "ORACLE_BROWSER_PROFILE_DIR", "ORACLE_BROWSER_CDP_URL", "ORACLE_BROWSER_LAUNCH_CDP",
)
_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def is_tailscale_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in _TAILSCALE_NETWORKS)


def _shared_browser_user_value(name: str) -> str | None:
    if sys.platform != "win32" or name not in _SHARED_BROWSER_ENV:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
        return value if kind in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} and isinstance(value, str) else None
    except OSError:
        return None


def _browser_getenv(name: str, default=None, *, getenv=os.getenv, allow_user_registry=True):
    value = getenv(name)
    if value is None and allow_user_registry:
        # Existing shells may predate the shared-profile deployment. Read only
        # these four non-secret user settings, never provider credentials.
        value = _shared_browser_user_value(name)
    return default if value is None else value


def _bool(name: str, default: bool, *, getenv=os.getenv) -> bool:
    value = getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float, *, getenv=os.getenv) -> float:
    value = getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _int(name: str, default: int, *, getenv=os.getenv) -> int:
    value = getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _csv(name: str, *, getenv=os.getenv) -> tuple[str, ...]:
    value = getenv(name)
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
    # Extra High turns may sit past an hour with no progress; a 1800 s budget
    # reported them as TIMEOUT although the page never left the turn.
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
    tailscale_ips: tuple[str, ...] = ()
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
    provider: str = "chatgpt"
    shared_browser: bool = False
    # After this many consecutive polls without any generation evidence, the
    # poller records idle evidence (and the TIMEOUT reason) explicitly.
    idle_resync_polls: int = 4
    gemini_account_indexes: tuple[int, ...] = (0, 1)
    gemini_preferred_account_index: int | None = None

    @property
    def browser_pool_path(self) -> Path | None:
        if not self.shared_browser:
            return None
        return self.browser_profile_dir.parent / f"{self.browser_profile_dir.name}.oracle-pool.sqlite3"

    @classmethod
    def from_env(cls, root: Path | None = None, *, provider: str | None = None, _values: dict[str, str] | None = None) -> "Settings":
        from functools import partial
        getenv = os.getenv if _values is None else _values.get
        boolean = partial(_bool, getenv=getenv)
        integer = partial(_int, getenv=getenv)
        number = partial(_float, getenv=getenv)
        csv = partial(_csv, getenv=getenv)
        shared_getenv = partial(_browser_getenv, getenv=getenv, allow_user_registry=_values is None)
        shared_boolean = partial(_bool, getenv=shared_getenv)
        provider = provider or getenv("ORACLE_PROVIDER", "chatgpt")
        if provider == "gemini":
            return cls._gemini_from_env(root)
        if provider != "chatgpt":
            raise ValueError("provider must be chatgpt or gemini")
        root = Path(getenv("CHATGPT_ORACLE_ROOT", str(root or Path.cwd()))).resolve()
        jobs_dir = Path(getenv("CHATGPT_ORACLE_JOBS_DIR", str(root / "runtime" / "jobs"))).resolve()
        profile = Path(
            getenv(
                "CHATGPT_PROFILE_DIR",
                str(root / "runtime" / "chatgpt-browser-profile"),
            )
        ).resolve()
        shared_browser = shared_boolean("ORACLE_SHARED_BROWSER", True)
        if shared_browser:
            profile = Path(shared_getenv("ORACLE_BROWSER_PROFILE_DIR", str(root / "runtime" / "oracle-browser-profile"))).resolve()
        cdp_url = shared_getenv("ORACLE_BROWSER_CDP_URL", "http://127.0.0.1:9333") if shared_browser else getenv("CHATGPT_BROWSER_CDP_URL", "http://127.0.0.1:9222")
        executable_value = getenv("CHATGPT_CHROME_EXECUTABLE", "").strip()
        executable = Path(executable_value).resolve() if executable_value else None
        file_store_value = getenv("CHATGPT_FILE_STORE_DIR", "").strip()
        public_base_value = getenv("CHATGPT_ORACLE_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public_base_value and not public_base_value.startswith(("http://", "https://")):
            raise ValueError("CHATGPT_ORACLE_PUBLIC_BASE_URL must use http:// or https://")
        transport = getenv("CHATGPT_ORACLE_TRANSPORT", "stdio").strip().lower()
        if transport not in {"stdio", "http", "streamable-http"}:
            raise ValueError("CHATGPT_ORACLE_TRANSPORT must be stdio or http")
        mcp_path = getenv("CHATGPT_ORACLE_MCP_PATH", "/mcp").strip() or "/mcp"
        if not mcp_path.startswith("/"):
            mcp_path = "/" + mcp_path
        if len(mcp_path) > 1:
            mcp_path = mcp_path.rstrip("/")
        port = integer("CHATGPT_ORACLE_PORT", 8787)
        tailscale_ips = csv("CHATGPT_ORACLE_TAILSCALE_IPS")
        if any(not is_tailscale_address(value) for value in tailscale_ips):
            raise ValueError("ORACLE_TAILSCALE_IPS must contain this node's exact Tailscale IP addresses")
        tailscale_ips = tuple(dict.fromkeys(str(ipaddress.ip_address(value)) for value in tailscale_ips))
        tailscale_hosts = tuple(
            f"[{value}]:{port}" if ":" in value else f"{value}:{port}"
            for value in tailscale_ips
        )
        allowed_hosts = tuple(dict.fromkeys((
            *_DEFAULT_ALLOWED_HOSTS, *csv("CHATGPT_ORACLE_ALLOWED_HOSTS"), *tailscale_hosts,
        )))
        return cls(
            root=root,
            jobs_dir=jobs_dir,
            browser_profile_dir=profile,
            chrome_executable=executable,
            browser_headless=boolean("CHATGPT_BROWSER_HEADLESS", False),
            browser_start_minimized=boolean("CHATGPT_BROWSER_START_MINIMIZED", True),
            browser_cdp_url=cdp_url.strip() or None,
            browser_launch_cdp=shared_boolean("ORACLE_BROWSER_LAUNCH_CDP", True) if shared_browser else boolean("CHATGPT_BROWSER_LAUNCH_CDP", True),
            browser_timeout_seconds=number("CHATGPT_BROWSER_TIMEOUT_SECONDS", 30.0),
            consult_poll_seconds=number("CHATGPT_CONSULT_POLL_SECONDS", 30.0),
            critic_poll_seconds=number("CHATGPT_CRITIC_POLL_SECONDS", 30.0),
            research_poll_seconds=number("CHATGPT_RESEARCH_POLL_SECONDS", 180.0),
            consult_timeout_seconds=number("CHATGPT_CONSULT_TIMEOUT_SECONDS", 5400.0),
            critic_timeout_seconds=number("CHATGPT_CRITIC_TIMEOUT_SECONDS", 5400.0),
            research_timeout_seconds=number("CHATGPT_RESEARCH_TIMEOUT_SECONDS", 7200.0),
            host=getenv("CHATGPT_ORACLE_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
            transport=transport,
            mcp_path=mcp_path,
            bearer_token=getenv("CHATGPT_ORACLE_BEARER_TOKEN") or None,
            allow_insecure_remote=boolean("CHATGPT_ORACLE_ALLOW_INSECURE_REMOTE", False),
            allowed_hosts=allowed_hosts,
            tailscale_ips=tailscale_ips,
            upload_max_file_bytes=integer("CHATGPT_UPLOAD_MAX_FILE_BYTES", 25 * 1024 * 1024),
            upload_max_total_bytes=integer("CHATGPT_UPLOAD_MAX_TOTAL_BYTES", 64 * 1024 * 1024),
            upload_max_files=integer("CHATGPT_UPLOAD_MAX_FILES", 8),
            upload_ttl_seconds=number("CHATGPT_UPLOAD_TTL_SECONDS", 900.0),
            export_max_count=integer("CHATGPT_EXPORT_MAX_COUNT", 1000),
            export_inline_max_bytes=integer("CHATGPT_EXPORT_INLINE_MAX_BYTES", 512 * 1024),
            export_ttl_seconds=number("CHATGPT_EXPORT_TTL_SECONDS", 900.0),
            export_audit_retention_seconds=number(
                "CHATGPT_EXPORT_AUDIT_RETENTION_SECONDS", 90 * 24 * 60 * 60
            ),
            export_max_snapshot_bytes=integer("CHATGPT_EXPORT_MAX_SNAPSHOT_BYTES", 64 * 1024 * 1024),
            file_store_dir=Path(file_store_value).resolve() if file_store_value else None,
            file_store_max_file_bytes=integer("CHATGPT_FILE_STORE_MAX_FILE_BYTES", 512 * 1024 * 1024),
            file_store_max_total_bytes=integer("CHATGPT_FILE_STORE_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024),
            file_store_max_transfers=integer("CHATGPT_FILE_STORE_MAX_TRANSFERS", 4),
            file_store_max_chunk_bytes=integer("CHATGPT_FILE_STORE_MAX_CHUNK_BYTES", 8 * 1024 * 1024),
            file_store_io_chunk_bytes=integer("CHATGPT_FILE_STORE_IO_CHUNK_BYTES", 256 * 1024),
            file_store_ttl_seconds=number("CHATGPT_FILE_STORE_TTL_SECONDS", 2 * 60 * 60),
            file_store_metadata_retention_seconds=number(
                "CHATGPT_FILE_STORE_METADATA_RETENTION_SECONDS", 90 * 24 * 60 * 60
            ),
            public_base_url=public_base_value or None,
            sessions_dir=Path(getenv("CHATGPT_SESSIONS_DIR", str(root / "runtime" / "sessions"))).resolve(),
            session_ttl_seconds=number("CHATGPT_SESSION_TTL_SECONDS", 7200.0),
            shared_browser=shared_browser,
        )

    @classmethod
    def _gemini_from_env(cls, root: Path | None = None) -> "Settings":
        # Translate environment names into a private mapping, never mutate the
        # process environment or inherit another provider's runtime directories.
        from dataclasses import replace

        mapped = {key.replace("GEMINI_", "CHATGPT_", 1): value
                  for key, value in os.environ.items() if key.startswith("GEMINI_")}
        project = Path(mapped.get("CHATGPT_ORACLE_ROOT", str(root or Path.cwd()))).resolve()
        defaults = {
            "CHATGPT_ORACLE_ROOT": str(project),
            "CHATGPT_ORACLE_JOBS_DIR": str(project / "runtime/gemini/jobs"),
            "CHATGPT_SESSIONS_DIR": str(project / "runtime/gemini/sessions"),
            "CHATGPT_PROFILE_DIR": str(Path.home() / ".agent-browser/gemini-native-profile"),
            "CHATGPT_BROWSER_CDP_URL": "http://127.0.0.1:9333",
            "CHATGPT_BROWSER_LAUNCH_CDP": "0",
            "CHATGPT_ORACLE_PORT": "8788",
            "CHATGPT_FILE_STORE_MAX_FILE_BYTES": str(100 * 1024 * 1024),
            "CHATGPT_CONSULT_POLL_SECONDS": "2",
            "CHATGPT_CRITIC_POLL_SECONDS": "2",
            "CHATGPT_RESEARCH_POLL_SECONDS": "10",
        }
        # Parse a private mapping; other threads may read ChatGPT settings.
        values = {**defaults, **mapped}
        values.update({key: value for key in _SHARED_BROWSER_ENV if (value := _browser_getenv(key)) is not None})
        account_values = os.getenv("GEMINI_ACCOUNT_INDEXES", "0,1").split(",")
        if not account_values or any(not value.strip().isdigit() for value in account_values):
            raise ValueError("GEMINI_ACCOUNT_INDEXES must contain nonnegative account indices")
        indexes = tuple(dict.fromkeys(int(value.strip()) for value in account_values))
        preferred_value = os.getenv("GEMINI_PREFERRED_ACCOUNT_INDEX", "").strip()
        preferred = int(preferred_value) if preferred_value.isdigit() else None
        if preferred_value and preferred is None or preferred is not None and preferred not in indexes:
            raise ValueError("GEMINI_PREFERRED_ACCOUNT_INDEX must be one of GEMINI_ACCOUNT_INDEXES")
        return replace(cls.from_env(project, provider="chatgpt", _values=values), provider="gemini",
                       gemini_account_indexes=indexes, gemini_preferred_account_index=preferred)

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
        if any(not is_tailscale_address(value) for value in self.tailscale_ips):
            raise ValueError("ORACLE_TAILSCALE_IPS must contain this node's exact Tailscale IP addresses")
        if self.transport not in {"http", "streamable-http"}:
            return
        loopback = self.host in {"127.0.0.1", "localhost", "::1"}
        if not loopback and not self.bearer_token and not self.tailscale_ips and not self.allow_insecure_remote:
            raise ValueError(
                "Remote MCP requires ORACLE_BEARER_TOKEN or exact ORACLE_TAILSCALE_IPS; "
                "ORACLE_ALLOW_INSECURE_REMOTE=1 is only for a private test network"
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
