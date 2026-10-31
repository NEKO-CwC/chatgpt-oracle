"""Cross-process allocation guard for the one dedicated browser profile."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from .submission_diagnostics import submission_identity, submission_phase


MAX_BROWSER_TABS = 4
_ui_owners: ContextVar[dict] = ContextVar("profile_ui_owners", default={})


class ResourceLockTimeout(TimeoutError):
    """The profile lock did not become available within its admission budget."""


class UIAdmissionDeferred(ResourceLockTimeout):
    """Initial UI admission failed before any page/input/send mutation."""


class ProfileResources:
    def __init__(self, profile: Path, *, lock_timeout_seconds: float = 30.0):
        self.path = profile.parent / f"{profile.name}.oracle-resources.sqlite3"
        self.ui_path = profile.parent / f"{profile.name}.oracle-ui.sqlite3"
        self.ui_owner_path = self.ui_path.with_suffix(".owner.json")
        self._ui_owner = _ui_owners
        self.lock_timeout_seconds = lock_timeout_seconds

    def _owner_hint(self) -> dict | None:
        try:
            return json.loads(self.ui_owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    async def _begin(self, conn: sqlite3.Connection, *, kind="allocation", admission=False) -> None:
        budget = min(0.25, self.lock_timeout_seconds) if admission else self.lock_timeout_seconds
        deadline = asyncio.get_running_loop().time() + budget
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    owner = self._owner_hint() if kind == "ui" else None
                    submission_phase("ui_deferred" if admission else "lock_timeout",
                                     lock_kind=kind, wait_seconds=budget, owner_hint=owner)
                    error = UIAdmissionDeferred if admission else ResourceLockTimeout
                    raise error(f"Browser {kind} lock unavailable after {budget:g} seconds; owner_hint={owner}") from exc
                await asyncio.sleep(min(0.05, remaining))

    @asynccontextmanager
    async def ui_locked(self, *, admission=False):
        """Serialize short foreground interactions, including other service processes.

        The separate database deliberately leaves page allocation and long-running
        generation/polling free. A task may nest UI helpers without deadlocking;
        child tasks do not inherit ownership of the lock.
        """
        task = asyncio.current_task()
        if task is not None and self._ui_owner.get().get(self.ui_path) is task:
            yield
            return
        self.ui_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.ui_path, timeout=0, isolation_level=None)
        token = None
        try:
            if admission:
                submission_phase("waiting_ui")
            await self._begin(conn, kind="ui", admission=admission)
            token = self._ui_owner.set({**self._ui_owner.get(), self.ui_path: task})
            try:
                self.ui_owner_path.write_text(json.dumps({"pid": os.getpid(), **submission_identity()}), encoding="utf-8")
            except OSError:
                pass
            if admission:
                submission_phase("ui_admitted")
            yield
        finally:
            if token is not None:
                self._ui_owner.reset(token)
                try:
                    self.ui_owner_path.unlink(missing_ok=True)
                except OSError:
                    pass
                conn.commit()
            conn.close()

    def ui_admitted(self):
        """Use only at the entry to a new submit/continue, before mutations."""
        return self.ui_locked(admission=True)

    @asynccontextmanager
    async def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            await self._begin(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS pages (
                target_id TEXT PRIMARY KEY,
                owner_pid INTEGER,
                retained INTEGER NOT NULL DEFAULT 0,
                retain_key TEXT,
                session_key TEXT,
                session_primary INTEGER NOT NULL DEFAULT 0
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pages)")}
            if "retain_key" not in columns:
                conn.execute("ALTER TABLE pages ADD COLUMN retain_key TEXT")
            if "session_key" not in columns:
                conn.execute("ALTER TABLE pages ADD COLUMN session_key TEXT")
            if "session_primary" not in columns:
                conn.execute("ALTER TABLE pages ADD COLUMN session_primary INTEGER NOT NULL DEFAULT 0")
                # Prior versions retained only the primary page after cleanup.
                # Prefer it when recovering an in-flight operation's records.
                conn.execute("""UPDATE pages SET session_primary=1
                    WHERE session_key IS NOT NULL AND target_id=(
                        SELECT prior.target_id FROM pages AS prior
                        WHERE prior.session_key=pages.session_key
                        ORDER BY prior.retained DESC,prior.rowid LIMIT 1
                    )""")
            try:
                yield conn
            finally:
                # Page creation/close happened in Chrome, outside SQLite. Keep
                # ownership records even when the surrounding operation fails.
                conn.commit()
        finally:
            conn.close()
