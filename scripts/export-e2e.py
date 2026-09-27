from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

MAX_PAGE_RESULT_BYTES = 15 * 1024
MAX_HTTP_RESPONSE_BYTES = 16 * 1024


def _structured(result: Any) -> dict[str, Any]:
    if result.isError:
        raise RuntimeError("MCP tool returned an error")
    value = result.structuredContent
    if isinstance(value, dict):
        if value.get("accepted") is False:
            raise RuntimeError(str(value.get("error_code") or "MCP tool rejected the request"))
        return value
    text_blocks = [item.text for item in result.content if hasattr(item, "text")]
    if len(text_blocks) == 1:
        value = json.loads(text_blocks[0])
        if isinstance(value, dict) and value.get("accepted") is False:
            raise RuntimeError(str(value.get("error_code") or "MCP tool rejected the request"))
        if isinstance(value, dict):
            return value
    raise RuntimeError("MCP tool did not return a structured object")


async def _wait_ready(session: ClientSession, session_id: str, export_id: str, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        status = _structured(
            await session.call_tool(
                "chatgpt_export_status",
                {"session_id": session_id, "export_id": export_id},
            )
        )
        if status.get("state") not in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
            return status
        retry_after = status.get("retry_after_seconds", 2)
        delay = min(max(float(retry_after or 2), 1.0), 5.0)
        await asyncio.sleep(delay)
    raise TimeoutError("export did not reach a terminal state before the deadline")


async def _collect_view(
    session: ClientSession,
    *,
    session_id: str,
    export_id: str,
    view: str,
    total_bytes: int,
    expected_sha256: str,
    deadline: float,
) -> tuple[dict[str, Any], bytes]:
    cursor: str | None = None
    offset = 0
    chunks: list[bytes] = []
    pages = 0
    max_page_result_bytes = 0
    while time.monotonic() < deadline:
        page = _structured(
            await session.call_tool(
                "chatgpt_export_page",
                {
                    "session_id": session_id,
                    "export_id": export_id,
                    "view": view,
                    "cursor": cursor,
                    "max_response_bytes": MAX_PAGE_RESULT_BYTES,
                },
            )
        )
        max_page_result_bytes = max(
            max_page_result_bytes,
            len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        )
        if page.get("byte_offset") != offset:
            raise RuntimeError(f"{view} page offset is not contiguous")
        chunk = page.get("chunk")
        if not isinstance(chunk, str):
            raise RuntimeError(f"{view} page has no UTF-8 chunk")
        encoded = chunk.encode("utf-8")
        if page.get("returned_bytes") != len(encoded):
            raise RuntimeError(f"{view} page byte count does not match its chunk")
        chunk_hash = hashlib.sha256(encoded).hexdigest()
        if page.get("chunk_sha256") != chunk_hash:
            raise RuntimeError(f"{view} page checksum mismatch")
        chunks.append(encoded)
        offset += len(encoded)
        pages += 1
        cursor = page.get("next_cursor")
        if page.get("eof"):
            if cursor is not None:
                raise RuntimeError(f"{view} final page has a next cursor")
            break
        if not cursor:
            raise RuntimeError(f"{view} page omitted its continuation cursor")
    else:
        raise TimeoutError(f"{view} pagination exceeded the deadline")

    content = b"".join(chunks)
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != total_bytes or digest != expected_sha256:
        raise RuntimeError(f"{view} reconstructed bytes or checksum differ from the snapshot")
    return (
        {
            "pages": pages,
            "bytes": len(content),
            "sha256": digest,
            "max_result_bytes": max_page_result_bytes,
        },
        content,
    )


async def _download_listed_attachments(
    session: ClientSession,
    *,
    mcp_url: str,
    token: str,
    session_id: str,
    export_id: str,
    attachments: list[dict[str, Any]],
    deadline: float,
) -> dict[str, Any]:
    available = [item for item in attachments if item.get("available") is True]
    candidates = [item for item in attachments if item.get("download_candidate") is True]
    attempts = [*available, *candidates]
    unavailable_count = len(attachments) - len(attempts)
    downloaded_bytes = 0
    downloaded = 0
    failures: list[str] = []
    verified_files: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60.0) as client:
        for item in attempts:
            if time.monotonic() >= deadline:
                raise TimeoutError("attachment transfer exceeded the export deadline")
            attachment_id = item.get("attachment_id")
            if not isinstance(attachment_id, str) or not attachment_id:
                failures.append("attachment_id_missing")
                continue
            try:
                handle = _structured(
                    await session.call_tool(
                        "chatgpt_export_attachment_handle",
                        {
                            "session_id": session_id,
                            "export_id": export_id,
                            "attachment_id": attachment_id,
                        },
                    )
                )
                download_url = handle.get("download_url")
                if not isinstance(download_url, str) or not download_url:
                    raise RuntimeError("download_url_missing")
                response = await client.get(urljoin(mcp_url, download_url))
                if response.status_code != 200:
                    raise RuntimeError(f"http_{response.status_code}")
                content = response.content
                digest = hashlib.sha256(content).hexdigest()
                declared_length = int(response.headers.get("content-length", "-1"))
                if declared_length != len(content):
                    raise RuntimeError("content_length_mismatch")
                if response.headers.get("x-content-sha256") != digest:
                    raise RuntimeError("content_sha256_mismatch")
                expected_size = item.get("size")
                if isinstance(expected_size, int) and expected_size >= 0 and len(content) != expected_size:
                    raise RuntimeError("manifest_size_mismatch")
                expected_mime = item.get("mime_type")
                actual_mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if isinstance(expected_mime, str) and expected_mime and actual_mime != expected_mime.split(";", 1)[0].strip().lower():
                    raise RuntimeError("content_type_mismatch")
                downloaded_bytes += len(content)
                downloaded += 1
                verified_files.append({"bytes": len(content), "sha256": digest})
            except Exception as exc:
                failures.append(type(exc).__name__ if not str(exc).startswith("http_") else str(exc))
    return {
        "listed_count": len(attachments),
        "available_count": len(available),
        "candidate_count": len(candidates),
        "attempted_count": len(attempts),
        "downloaded_count": downloaded,
        "downloaded_bytes": downloaded_bytes,
        "verified_files": verified_files,
        "unavailable_count": unavailable_count,
        "failure_count": len(failures),
        "failure_types": failures,
        "inventory_completeness": "unverified",
    }


def _attachment_reference_summary(messages: list[dict[str, Any]]) -> dict[str, Any]:
    attachment_kinds: Counter[str] = Counter()
    rich_part_types: Counter[str] = Counter()
    reference_fields: Counter[str] = Counter()
    attachment_count = 0
    attachment_message_count = 0
    rich_part_count = 0
    tracked_fields = {
        "asset_pointer",
        "attachment_id",
        "file_id",
        "fileId",
        "file_name",
        "filename",
        "library_file_id",
        "sandbox_path",
    }

    for message in messages:
        attachments = message.get("attachments")
        if isinstance(attachments, list):
            valid_attachments = [item for item in attachments if isinstance(item, dict)]
            attachment_count += len(valid_attachments)
            attachment_message_count += bool(valid_attachments)
            for item in valid_attachments:
                kind = item.get("file_kind")
                attachment_kinds[str(kind) if isinstance(kind, str) and kind else "unspecified"] += 1
                for field in tracked_fields.intersection(item):
                    reference_fields[field] += 1

        rich_parts = message.get("rich_parts")
        if isinstance(rich_parts, list):
            for part in rich_parts:
                if not isinstance(part, dict):
                    continue
                rich_part_count += 1
                content_type = part.get("content_type")
                if isinstance(content_type, str) and content_type and len(content_type) <= 64:
                    rich_part_types[content_type] += 1
                for field in tracked_fields.intersection(part):
                    reference_fields[field] += 1

    return {
        "attachment_count": attachment_count,
        "attachment_message_count": attachment_message_count,
        "attachment_kind_counts": dict(sorted(attachment_kinds.items())),
        "rich_part_count": rich_part_count,
        "rich_part_type_counts": dict(sorted(rich_part_types.items())),
        "reference_field_counts": dict(sorted(reference_fields.items())),
    }


async def _collect_messages(
    session: ClientSession,
    *,
    session_id: str,
    export_id: str,
    total_bytes: int,
    expected_sha256: str,
    expected_count: int,
    deadline: float,
) -> dict[str, Any]:
    cursor: str | None = None
    records: dict[int, dict[str, Any]] = {}
    segments: dict[int, bytearray] = {}
    pages = 0
    max_page_result_bytes = 0

    while time.monotonic() < deadline:
        page = _structured(
            await session.call_tool(
                "chatgpt_export_page",
                {
                    "session_id": session_id,
                    "export_id": export_id,
                    "view": "messages",
                    "cursor": cursor,
                    "max_response_bytes": MAX_PAGE_RESULT_BYTES,
                },
            )
        )
        max_page_result_bytes = max(
            max_page_result_bytes,
            len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        )
        pages += 1
        segment = page.get("message_segment")
        if isinstance(segment, dict):
            ordinal = segment.get("ordinal")
            offset = segment.get("byte_offset")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                raise RuntimeError("message segment omitted its ordinal")
            assembled = segments.setdefault(ordinal, bytearray())
            if offset != len(assembled):
                raise RuntimeError("message segment offset is not contiguous")
            chunk = segment.get("chunk")
            if not isinstance(chunk, str):
                raise RuntimeError("message segment omitted its UTF-8 chunk")
            encoded = chunk.encode("utf-8")
            if segment.get("returned_bytes") != len(encoded):
                raise RuntimeError("message segment byte count does not match its chunk")
            if segment.get("chunk_sha256") != hashlib.sha256(encoded).hexdigest():
                raise RuntimeError("message segment checksum mismatch")
            assembled.extend(encoded)
            if segment.get("complete"):
                content = bytes(assembled)
                if len(content) != segment.get("total_bytes"):
                    raise RuntimeError("reassembled message byte count differs from its segment metadata")
                if hashlib.sha256(content).hexdigest() != segment.get("sha256"):
                    raise RuntimeError("reassembled message checksum mismatch")
                message = json.loads(content)
                if not isinstance(message, dict) or int(message.get("ordinal", -1)) != ordinal:
                    raise RuntimeError("reassembled message ordinal does not match its cursor")
                records[ordinal] = message
        else:
            messages = page.get("messages")
            if not isinstance(messages, list):
                raise RuntimeError("structured message page omitted its messages list")
            for message in messages:
                if not isinstance(message, dict):
                    raise RuntimeError("structured message page contains a non-object message")
                ordinal = message.get("ordinal")
                if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal in records:
                    raise RuntimeError("structured message ordinal is invalid or repeated")
                records[ordinal] = message

        cursor = page.get("next_cursor")
        if page.get("eof"):
            if cursor is not None:
                raise RuntimeError("final message page has a next cursor")
            break
        if not cursor:
            raise RuntimeError("message page omitted its continuation cursor")
    else:
        raise TimeoutError("messages pagination exceeded the deadline")

    if sorted(records) != list(range(expected_count)):
        raise RuntimeError("message ordinals are not complete and contiguous")
    ordered_messages = [records[index] for index in range(expected_count)]
    content = json.dumps(
        ordered_messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != total_bytes or digest != expected_sha256:
        raise RuntimeError("structured message pages differ from the snapshot byte count or checksum")
    return {
        "pages": pages,
        "bytes": len(content),
        "sha256": digest,
        "max_result_bytes": max_page_result_bytes,
        "attachment_reference_summary": _attachment_reference_summary(ordered_messages),
    }


@asynccontextmanager
async def _mcp_streams(args: argparse.Namespace, token: str):
    if args.transport == "stdio":
        root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "chatgpt_oracle.server"],
            env={
                **os.environ,
                "CHATGPT_ORACLE_ROOT": str(root),
                "CHATGPT_ORACLE_TRANSPORT": "stdio",
                "CHATGPT_ORACLE_BEARER_TOKEN": token,
                "PYTHONPATH": str(root),
            },
        )
        async with stdio_client(params) as streams:
            yield streams
        return

    client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async def capture_page_response(response: Any) -> None:
        try:
            request_body = json.loads(response.request.content)
            params = request_body.get("params", {})
            if request_body.get("method") == "tools/call" and params.get("name") == "chatgpt_export_page":
                args.http_page_response_bytes.append(len(await response.aread()))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return

    client.event_hooks["response"].append(capture_page_response)
    async with client:
        async with streamable_http_client(
            args.mcp_url,
            http_client=client,
            terminate_on_close=True,
        ) as streams:
            yield streams[0], streams[1]


async def run(args: argparse.Namespace) -> None:
    token = os.environ.get("CHATGPT_ORACLE_BEARER_TOKEN")
    if not token:
        raise RuntimeError("CHATGPT_ORACLE_BEARER_TOKEN is not configured in the environment")
    deadline = time.monotonic() + args.timeout
    args.http_page_response_bytes = []
    async with _mcp_streams(args, token) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            required = {
                "chatgpt_export_start",
                "chatgpt_export_status",
                "chatgpt_export_page",
                "chatgpt_export_attachment_handle",
                "chatgpt_export_release",
            }
            missing = sorted(required - names)
            if missing:
                raise RuntimeError("MCP server is missing V2 export tools: " + ", ".join(missing))

            started = _structured(
                await session.call_tool(
                    "chatgpt_export_start",
                    {
                        "conversation_url": args.conversation_url,
                        "scope": "all",
                        "request_id": "export-e2e-" + uuid.uuid4().hex,
                        "on_busy": "queue",
                    },
                )
            )
            session_id = str(started["session_id"])
            export_id = str(started["export_id"])
            try:
                status = await _wait_ready(session, session_id, export_id, deadline)
                if status.get("state") != "READY":
                    raise RuntimeError("export ended in state " + str(status.get("state")))
                views = status.get("views")
                if not isinstance(views, dict):
                    raise RuntimeError("ready export omitted view metadata")

                collected = {
                    "messages": await _collect_messages(
                        session,
                        session_id=session_id,
                        export_id=export_id,
                        total_bytes=int(views["messages"]["total_bytes"]),
                        expected_sha256=str(views["messages"]["sha256"]),
                        expected_count=int(status["message_count"]),
                        deadline=deadline,
                    )
                }
                attachment_records: list[dict[str, Any]] = []
                for view in ("attachments", "markdown"):
                    meta = views[view]
                    view_result, content = await _collect_view(
                        session,
                        session_id=session_id,
                        export_id=export_id,
                        view=view,
                        total_bytes=int(meta["total_bytes"]),
                        expected_sha256=str(meta["sha256"]),
                        deadline=deadline,
                    )
                    collected[view] = view_result
                    if view == "attachments":
                        value = json.loads(content)
                        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                            raise RuntimeError("attachment view is not a JSON object list")
                        attachment_records = value
                attachment_transfer = await _download_listed_attachments(
                    session,
                    mcp_url=args.mcp_url,
                    token=token,
                    session_id=session_id,
                    export_id=export_id,
                    attachments=attachment_records,
                    deadline=deadline,
                )
                attachment_completeness = (
                    status.get("completeness", {}).get("attachments")
                    if isinstance(status.get("completeness"), dict)
                    else "unverified"
                )
                attachment_transfer["inventory_completeness"] = attachment_completeness
                attachment_kinds = Counter(
                    str(item.get("file_kind") or "unspecified")
                    for item in attachment_records
                )
                available_by_kind = Counter(
                    str(item.get("file_kind") or "unspecified")
                    for item in attachment_records
                    if item.get("available") is True
                )
                attachment_inventory_by_kind = {
                    kind: {
                        "listed": listed_count,
                        "available": available_by_kind.get(kind, 0),
                        "unavailable": listed_count - available_by_kind.get(kind, 0),
                    }
                    for kind, listed_count in sorted(attachment_kinds.items())
                }
                max_result_bytes = max(item["max_result_bytes"] for item in collected.values())
                max_http_bytes = max(args.http_page_response_bytes, default=None)
                if (
                    max_result_bytes > MAX_PAGE_RESULT_BYTES
                    or max_http_bytes is not None
                    and max_http_bytes > MAX_HTTP_RESPONSE_BYTES
                ):
                    raise RuntimeError("paged export exceeded the measured MCP response ceiling")
                report = {
                    "state": status["state"],
                    "source": status.get("source"),
                    "completeness": status.get("completeness"),
                    "message_count": status.get("message_count"),
                    "attachment_count": status.get("attachment_count"),
                    "attachment_inventory_by_kind": attachment_inventory_by_kind,
                    "message_attachment_references": collected["messages"].pop("attachment_reference_summary"),
                    "attachment_transfer": attachment_transfer,
                    "views": collected,
                    "max_page_result_bytes": max_result_bytes,
                    "max_http_page_response_bytes": max_http_bytes,
                }
                print(json.dumps(report, ensure_ascii=False, indent=2))
                if (
                    attachment_transfer["unavailable_count"]
                    or attachment_transfer["failure_count"]
                    or attachment_completeness != "verified"
                ):
                    raise RuntimeError("attachment inventory or one or more file transfers are not verified")
            finally:
                await session.call_tool(
                    "chatgpt_export_release",
                    {"session_id": session_id, "export_id": export_id},
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify paged export transport without printing conversation content.")
    parser.add_argument("conversation_url")
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8787/mcp")
    parser.add_argument("--timeout", type=float, default=900.0)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
