from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .exports import ExportCapacityError, _normalise_messages, _safe, _without_download_sources


@dataclass
class _PageRef:
    path: Path
    size: int
    raw_count: int
    previous: bool | None
    next_page: bool | None
    cursor_matches: bool
    excluded_system_count: int
    attachments: list[dict[str, Any]]


class PagedMessageSpool:
    """Disk-backed, re-iterable normalized messages staged one API page at a time."""

    is_message_spool = True

    def __init__(self, root: Path, *, max_bytes: int):
        root.mkdir(parents=True, exist_ok=True)
        self._temporary_root = tempfile.TemporaryDirectory(
            prefix=f"ingest-{os.getpid()}-",
            dir=str(root),
        )
        self._root = Path(self._temporary_root.name)
        self._max_bytes = max_bytes
        self._pages: list[_PageRef] = []
        self._message_count = 0
        self._source_page_bytes = 0
        self._staged_page_bytes = 0
        self._summary: dict[str, Any] | None = None
        self._attachments: list[dict[str, Any]] = []
        self._closed = False

    def append_page(
        self,
        payload: dict[str, Any],
        normalized: dict[str, Any],
        *,
        cursor_matches: bool,
        source_page_bytes: int | None = None,
    ) -> None:
        if self._closed or self._summary is not None:
            raise RuntimeError("message spool is finalized")
        raw_messages = payload.get("messages")
        normalized_messages = normalized.get("messages")
        if not isinstance(raw_messages, list) or not isinstance(normalized_messages, list):
            raise ValueError("source page messages must be lists")
        raw_page_bytes = self.measure_page(payload) if source_page_bytes is None else source_page_bytes
        if raw_page_bytes < 0 or self._source_page_bytes + raw_page_bytes > self._max_bytes:
            raise ExportCapacityError("source API page staging limit reached")
        messages = _normalise_messages(normalized_messages)

        source_hashes: dict[str, deque[tuple[str | None, str]]] = defaultdict(deque)
        raw_count = len(raw_messages)
        for index, raw_message in enumerate(raw_messages):
            if not isinstance(raw_message, dict):
                continue
            message_id = raw_message.get("id") or raw_message.get("message_id")
            normalized_id = str(message_id or f"msg_{index + 1}")
            source_id = str(message_id) if isinstance(message_id, str) and message_id else None
            digest = hashlib.sha256()
            encoder = json.JSONEncoder(
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            for part in encoder.iterencode(raw_message):
                digest.update(part.encode("utf-8"))
            source_hashes[normalized_id].append((source_id, digest.hexdigest()))

        page_index = len(self._pages)
        page_path = self._root / f"page-{page_index:08d}.jsonl"
        page_bytes = 0
        try:
            with page_path.open("wb") as output:
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    message_id = str(message.get("id") or message.get("message_id") or "")
                    source_id, source_hash = source_hashes[message_id].popleft() if source_hashes[message_id] else (None, "")
                    record = {
                        "source_id": source_id,
                        "source_sha256": source_hash,
                        "message": _safe(_without_download_sources(message)),
                    }
                    encoded = json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"
                    page_bytes += len(encoded)
                    if self._staged_page_bytes + page_bytes > self._max_bytes:
                        raise ExportCapacityError("source page staging limit reached")
                    output.write(encoded)
        except Exception:
            page_path.unlink(missing_ok=True)
            raise
        self._source_page_bytes += raw_page_bytes
        self._staged_page_bytes += page_bytes

        page_info = payload.get("page_info") if isinstance(payload.get("page_info"), dict) else {}
        attachments = normalized.get("attachments")
        page_attachments = [dict(item) for item in attachments if isinstance(item, dict)] if isinstance(attachments, list) else []
        excluded_system_count = normalized.get("excluded_system_message_count")
        self._pages.append(
            _PageRef(
                path=page_path,
                size=page_bytes,
                raw_count=raw_count,
                previous=page_info.get("has_previous_page") if isinstance(page_info.get("has_previous_page"), bool) else None,
                next_page=page_info.get("has_next_page") if isinstance(page_info.get("has_next_page"), bool) else None,
                cursor_matches=cursor_matches,
                excluded_system_count=excluded_system_count if isinstance(excluded_system_count, int) else 0,
                attachments=page_attachments,
            )
        )

    def measure_page(self, payload: dict[str, Any]) -> int:
        raw_page_bytes = 0
        page_encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), default=str)
        for part in page_encoder.iterencode(payload):
            raw_page_bytes += len(part.encode("utf-8"))
            if self._source_page_bytes + raw_page_bytes > self._max_bytes:
                raise ExportCapacityError("source API page staging limit reached")
        return raw_page_bytes

    def finalize(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("message spool is closed")
        if self._summary is not None:
            return dict(self._summary)
        if not self._pages:
            raise ValueError("message spool has no source pages")

        ordered_pages = [*reversed(self._pages[1:]), self._pages[0]]
        page_flags: list[dict[str, bool | None]] = []
        raw_message_count = 0
        cursor_mismatches = 0
        duplicate_count = 0
        conflicting_duplicate = False
        excluded_system_count = 0
        for page in ordered_pages:
            raw_message_count += page.raw_count
            excluded_system_count += page.excluded_system_count
            page_flags.append({
                "has_previous_page": page.previous,
                "has_next_page": page.next_page,
            })
            if not page.cursor_matches:
                cursor_mismatches += 1
            self._attachments.extend(page.attachments)

        self._message_count = sum(1 for _ in self._iter_unique_records())
        duplicate_count = self._duplicate_count
        conflicting_duplicate = self._conflicting_duplicate

        oldest_reached = page_flags[0]["has_previous_page"] is False
        newest_reached = page_flags[-1]["has_next_page"] is False
        known_more = any(
            flag["has_previous_page"] is True or flag["has_next_page"] is True
            for flag in (page_flags[0], page_flags[-1])
        )
        if cursor_mismatches or conflicting_duplicate:
            completeness = "partial"
            reason = "api_page_chain_inconsistent"
        elif oldest_reached and newest_reached:
            completeness = "verified"
            reason = "api_page_boundaries_reached"
        elif known_more:
            completeness = "partial"
            reason = "api_pages_incomplete"
        else:
            completeness = "unverified"
            reason = "api_page_boundaries_unknown"

        self._summary = {
            "messages": self,
            "attachments": self._attachments,
            "pagination": {
                "page_count": len(page_flags),
                "raw_message_count": raw_message_count,
                "page_flags": page_flags,
                "cursor_mismatch_count": cursor_mismatches,
                "duplicate_count": duplicate_count,
                "conflicting_duplicate": conflicting_duplicate,
                "completeness": completeness,
                "reason": reason,
            },
            "excluded_system_message_count": excluded_system_count,
        }
        return dict(self._summary)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if self._summary is None:
            raise RuntimeError("message spool must be finalized before reading")
        for ordinal, record in enumerate(self._iter_unique_records()):
            message = record["message"]
            message["order"] = ordinal
            message.setdefault("message_id", str(message.get("id") or f"msg_{ordinal + 1}"))
            yield message

    def _iter_unique_records(self) -> Iterator[dict[str, Any]]:
        seen: dict[str, str] = {}
        self._duplicate_count = 0
        self._conflicting_duplicate = False
        for page in [*reversed(self._pages[1:]), self._pages[0]]:
            with page.path.open("r", encoding="utf-8") as source:
                for line in source:
                    record = json.loads(line)
                    source_id = record.get("source_id")
                    source_hash = str(record.get("source_sha256") or "")
                    if isinstance(source_id, str) and source_id:
                        previous_hash = seen.get(source_id)
                        if previous_hash is not None:
                            self._duplicate_count += 1
                            if previous_hash != source_hash:
                                self._conflicting_duplicate = True
                            continue
                        seen[source_id] = source_hash
                    yield record

    @property
    def staged_bytes(self) -> int:
        return self._staged_page_bytes

    def __len__(self) -> int:
        return self._message_count

    def __bool__(self) -> bool:
        return self._message_count > 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._temporary_root.cleanup()
