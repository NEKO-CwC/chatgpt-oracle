"""Non-secret, atomic account preference shared by Gemini operations.

This stores URL indices, not Google identities or credentials. Browser callers
serialize read/modify/write with the existing profile resource lock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


class GeminiAccountState:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("observed_routes", {}), dict):
                return value
        except (OSError, ValueError):
            pass
        return {"observed_routes": {}}

    @staticmethod
    def explicit_selection(state: dict) -> dict | None:
        """Return only a well-formed caller selection, separate from auto hints."""
        value = state.get("explicit_selection")
        if (isinstance(value, dict) and type(value.get("account_index")) is int
                and value["account_index"] >= 0 and type(value.get("strict")) is bool):
            return dict(value)
        return None

    def select(self, account_index: int | None, *, strict: bool, selected_at: str) -> dict:
        """Caller holds the profile allocation lock across this whole update."""
        state = self.read()
        if account_index is None:
            state.pop("explicit_selection", None)
        else:
            state["explicit_selection"] = {
                "account_index": account_index, "strict": strict, "selected_at": selected_at,
            }
        self.write(state)
        return state

    def write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="gemini-accounts-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
