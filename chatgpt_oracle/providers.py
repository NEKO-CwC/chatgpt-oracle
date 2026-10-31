"""Small provider policy; browser behavior and durable stores remain separate."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import ParseResult, urlparse, urlunparse

from .file_store import FileTransferError


@dataclass(frozen=True)
class ProviderPolicy:
    name: str
    title: str
    qualities: tuple[str, ...]
    defaults: dict[str, str]
    hosts: frozenset[str]
    conversation_prefix: str
    source_attachments: bool = True

    def canonical_url(self, value: str) -> str:
        if self.name == "gemini":
            parsed, _, conversation_id = _gemini_route(value)
            if conversation_id is None:
                raise ValueError("conversation_url must identify one Gemini conversation")
            return urlunparse(("https", "gemini.google.com", parsed.path.rstrip("/"), "", "", ""))
        parsed = urlparse(value.strip())
        if (parsed.scheme != "https" or parsed.hostname not in self.hosts
                or parsed.username or parsed.password or parsed.port
                or not parsed.path.startswith(self.conversation_prefix)
                or len(parsed.path.rstrip("/").split("/")) != 3):
            raise ValueError(f"conversation_url must identify one {self.title} conversation")
        return urlunparse(("https", parsed.hostname, parsed.path.rstrip("/"), "", "", ""))

    def validate_file(self, filename: str, mime_type: str, size: int) -> None:
        if self.name != "gemini":
            return
        if filename.lower().endswith(".zip") or mime_type.lower().split(";")[0] in {
            "application/zip", "application/x-zip", "application/x-zip-compressed",
        }:
            raise FileTransferError("UNSUPPORTED_UPLOAD_TYPE", "Gemini ZIP inputs are not supported")
        if size > 100 * 1024 * 1024:
            raise FileTransferError("FILE_TOO_LARGE", "Gemini inputs are limited to 100 MiB per file", 413)


CHATGPT = ProviderPolicy(
    "chatgpt", "ChatGPT", ("auto", "extra_high", "pro"),
    {"consult": "extra_high", "critic": "extra_high", "research": "pro"},
    frozenset({"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"}), "/c/",
)
GEMINI = ProviderPolicy(
    "gemini", "Gemini", ("auto", "low", "medium", "high"),
    {"consult": "high", "critic": "high", "research": "high"},
    frozenset({"gemini.google.com"}), "/app/", source_attachments=False,
)


_GEMINI_ROUTE = re.compile(
    r"^/(?:u/(?P<account>0|[1-9][0-9]*)/)?app(?:/(?P<conversation>[A-Za-z0-9_-]+))?/?$"
)


def _gemini_route(value: str) -> tuple[ParseResult, int, str | None]:
    """Parse a home or conversation route without erasing its account identity."""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("URL must identify a Gemini account home or conversation")
    parsed = urlparse(value.strip())
    match = _GEMINI_ROUTE.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "gemini.google.com"
            or parsed.params or not match):
        raise ValueError("URL must identify a Gemini account home or conversation")
    return parsed, int(match.group("account") or 0), match.group("conversation")


def gemini_account_index(value: str) -> int:
    """Return the URL's account index; a legacy /app route means default account 0.

    This comparison convention never rewrites stored legacy conversation URLs.
    """
    return _gemini_route(value)[1]


def gemini_account_home(account_index: int) -> str:
    """Construct an explicit account home for a new, account-pinned page."""
    if type(account_index) is not int or account_index < 0:
        raise ValueError("Gemini account index must be a nonnegative integer")
    return f"https://gemini.google.com/u/{account_index}/app?pli=1"


def gemini_same_account(left: str, right: str) -> bool:
    """Compare validated Gemini routes, treating legacy /app as account 0."""
    return gemini_account_index(left) == gemini_account_index(right)


def provider_policy(name: str) -> ProviderPolicy:
    try:
        return {"chatgpt": CHATGPT, "gemini": GEMINI}[name]
    except KeyError as exc:
        raise ValueError("provider must be chatgpt or gemini") from exc
