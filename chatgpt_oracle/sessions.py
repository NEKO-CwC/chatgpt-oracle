"""Durable local mappings between caller sessions and ChatGPT conversations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .oracle_protocol import BOOTSTRAP_VERSION, MEMORY_POLICY, ORIGIN_MARKER, PROTOCOL_VERSION


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class ConversationSession:
    id: str
    conversation_url: str | None = None
    last_task_id: str | None = None
    state: str = "LIVE"
    created_at: str = ""
    last_used_at: str = ""
    expires_at: str = ""
    closed_at: str | None = None
    tab_cleanup_pending: bool = False
    oracle_protocol_version: str | None = None
    oracle_bootstrap_version: str | None = None

    @classmethod
    def create(cls, session_id: str, ttl_seconds: float) -> "ConversationSession":
        now = _now()
        return cls(
            id=session_id,
            created_at=_iso(now),
            last_used_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
            oracle_protocol_version=PROTOCOL_VERSION,
            oracle_bootstrap_version=BOOTSTRAP_VERSION,
        )

    def touch(self, ttl_seconds: float) -> None:
        now = _now()
        self.state = "LIVE"
        self.last_used_at = _iso(now)
        self.expires_at = _iso(now + timedelta(seconds=ttl_seconds))
        self.closed_at = None

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and _parse(self.expires_at) <= _now()

    def public(self, *, tab_open: bool | None = None) -> dict[str, Any]:
        value = {
            "session_id": self.id,
            "conversation_url": self.conversation_url,
            "last_task_id": self.last_task_id,
            "state": self.state,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "expires_at": self.expires_at,
            "closed_at": self.closed_at,
            "oracle_protocol": {
                "configured_version": self.oracle_protocol_version,
                "configured_bootstrap_version": self.oracle_bootstrap_version,
                "origin_marker": ORIGIN_MARKER if self.oracle_protocol_version else None,
                "memory_policy": MEMORY_POLICY,
                # Configuring a logical Session does not prove a Web send.
                "bootstrap": {"state": "unknown", "version": None, "task_id": None, "user_message_id": None, "verified_at": None},
            },
        }
        if tab_open is not None:
            value["tab_open"] = tab_open
        return value


class SessionStore:
    """One atomic JSON file per logical session; no conversation body is stored."""

    def __init__(self, directory: Path, ttl_seconds: float = 7200.0):
        self.directory = directory
        self.ttl_seconds = ttl_seconds
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_id(session_id: str) -> str:
        session_id = session_id.strip()
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("session_id must be 1-128 characters using letters, numbers, '.', '_', ':' or '-'")
        return session_id

    def _path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"session_{digest}.json"

    def create(self, session_id: str | None = None) -> ConversationSession:
        value = self.validate_id(session_id) if session_id else f"cgs_{uuid.uuid4().hex[:20]}"
        existing = self.get(value)
        if existing:
            return existing
        session = ConversationSession.create(value, self.ttl_seconds)
        self.save(session)
        return session

    def get(self, session_id: str) -> ConversationSession | None:
        value = self.validate_id(session_id)
        path = self._path(value)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = ConversationSession(**{
                key: item for key, item in data.items()
                if key in ConversationSession.__dataclass_fields__
            })
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return session if session.id == value else None

    def save(self, session: ConversationSession) -> None:
        path = self._path(session.id)
        fd, temp_name = tempfile.mkstemp(prefix="session.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(session), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def touch(
        self,
        session_id: str,
        *,
        conversation_url: str | None = None,
        last_task_id: str | None = None,
    ) -> ConversationSession:
        session = self.get(session_id) or self.create(session_id)
        if conversation_url:
            session.conversation_url = conversation_url
        if last_task_id:
            session.last_task_id = last_task_id
        session.touch(self.ttl_seconds)
        self.save(session)
        return session

    def all(self) -> list[ConversationSession]:
        sessions: list[ConversationSession] = []
        for path in self.directory.glob("session_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session = ConversationSession(**{
                    key: item for key, item in data.items()
                    if key in ConversationSession.__dataclass_fields__
                })
                self.validate_id(session.id)
                sessions.append(session)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return sorted(sessions, key=lambda item: item.last_used_at, reverse=True)

    def expire_inactive(self, *, exclude: set[str] | None = None) -> list[ConversationSession]:
        exclude = exclude or set()
        expired: list[ConversationSession] = []
        now = _now()
        for session in self.all():
            if session.id in exclude or session.state != "LIVE" or not session.expires_at:
                continue
            try:
                expiry = _parse(session.expires_at)
            except ValueError:
                continue
            if expiry > now:
                continue
            session.state = "DEAD"
            session.closed_at = _iso(now)
            session.tab_cleanup_pending = True
            self.save(session)
            expired.append(session)
        return expired

    def close(self, session_id: str) -> ConversationSession:
        session = self.get(session_id)
        if not session:
            raise KeyError(f"session not found: {session_id}")
        session.state = "DEAD"
        session.closed_at = _iso(_now())
        session.tab_cleanup_pending = True
        self.save(session)
        return session


__all__ = ["ConversationSession", "SessionStore"]
