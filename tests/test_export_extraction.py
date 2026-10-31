from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from chatgpt_oracle.browser import (
    ChatGPTBrowser,
    _attachment_scan_reaches_api_count,
    _api_attachment_metadata_error,
    _api_attachment_reference_summary,
    _collect_older_api_pages,
    _conversation_messages_request,
    _file_download_attachments_from_responses,
    _file_download_resource_urls,
    _has_authenticated_conversation_response,
    _is_explicit_login_page,
    _interpreter_metadata_resource_urls,
    _sandbox_attachment_references,
)
from chatgpt_oracle.config import Settings


def test_conversation_messages_request_reuses_client_headers_and_cursor() -> None:
    latest_url = (
        "https://chatgpt.com/backend-api/conversations/test-id"
        "?include_has_versions=true&num_turns=28"
    )
    headers = {
        "Authorization": "Bearer test-token",
        "oai-session-id": "test-session",
        "X-OpenAI-Target-Path": "/backend-api/conversations/test-id",
        "X-OpenAI-Target-Route": "/backend-api/conversations/test-id",
        "Cookie": "must-not-be-forwarded",
        "Sec-CH-UA": "must-not-be-forwarded",
    }

    request_url, request_headers = _conversation_messages_request(
        "https://chatgpt.com/c/test-id",
        "test-id",
        latest_url,
        "cursor-token",
        headers,
    )
    request_query = parse_qs(urlparse(request_url).query)

    assert urlparse(request_url).path == "/backend-api/conversations/test-id/messages"
    assert request_query == {
        "before": ["cursor-token"],
        "include_has_versions": ["true"],
        "num_turns": ["28"],
    }
    assert request_headers["authorization"] == "Bearer test-token"
    assert request_headers["oai-session-id"] == "test-session"
    assert request_headers["x-openai-target-path"] == "/backend-api/conversations/test-id/messages"
    assert request_headers["x-openai-target-route"] == "/backend-api/conversations/test-id/messages"
    assert "cookie" not in request_headers
    assert "sec-ch-ua" not in request_headers


def test_conversation_messages_request_builds_route_headers_from_validated_target() -> None:
    request_url, request_headers = _conversation_messages_request(
        "https://chatgpt.com/c/test-id",
        "test-id",
        "https://chatgpt.com/backend-api/conversations/test-id?include_has_versions=true",
        "cursor-token",
        {
            "authorization": "Bearer test-token",
            "x-openai-target-path": "/backend-api/conversations/another-id",
            "x-openai-target-route": "/backend-api/conversations/another-id",
            "cookie": "must-not-be-forwarded",
        },
    )

    expected_path = "/backend-api/conversations/test-id/messages"
    assert request_url.startswith(f"https://chatgpt.com{expected_path}?")
    assert request_headers["x-openai-target-path"] == expected_path
    assert request_headers["x-openai-target-route"] == expected_path
    assert request_headers["authorization"] == "Bearer test-token"
    assert "cookie" not in request_headers


def test_conversation_messages_request_rejects_mismatched_conversation() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _conversation_messages_request(
            "https://chatgpt.com/c/other-id",
            "test-id",
            "https://chatgpt.com/backend-api/conversations/test-id?include_has_versions=true",
            "cursor-token",
            {
                "x-openai-target-path": "/backend-api/conversations/test-id",
                "x-openai-target-route": "/backend-api/conversations/test-id",
            },
        )


def test_api_mapping_follows_active_parent_chain_and_excludes_other_branch() -> None:
    payload = {
        "current_node": "assistant-leaf",
        "mapping": {
            "root": {
                "id": "root",
                "parent": None,
                "message": {"id": "user-1", "author": {"role": "user"}, "content": {"parts": ["first"]}},
            },
            "stale-branch": {
                "id": "stale-branch",
                "parent": "root",
                "message": {"id": "assistant-stale", "author": {"role": "assistant"}, "content": {"parts": ["stale"]}},
            },
            "assistant-leaf": {
                "id": "assistant-leaf",
                "parent": "user-2-node",
                "message": {"id": "assistant-2", "author": {"role": "assistant"}, "content": {"parts": ["last"]}},
            },
            "user-2-node": {
                "id": "user-2-node",
                "parent": "root",
                "message": {"id": "user-2", "author": {"role": "user"}, "content": {"parts": ["second"]}},
            },
        },
    }
    result = ChatGPTBrowser._normalise_api_conversation(payload, "https://chatgpt.com/c/test")

    assert [message["id"] for message in result["messages"]] == [
        "user-1",
        "user-2",
        "assistant-2",
    ]
    assert [message["order"] for message in result["messages"]] == [0, 1, 2]
    assert result["branch_truncated"] is False
    assert result["branch_selection"] == "active_parent_chain"


def test_api_message_list_preserves_source_order() -> None:
    payload = {
        "messages": [
            {"id": "later-created", "role": "assistant", "text": "first in source"},
            {"id": "earlier-created", "role": "user", "text": "second in source"},
        ],
        "attachments": [{"id": "att-1", "filename": "file.txt"}],
    }
    result = ChatGPTBrowser._normalise_api_conversation(payload, "https://chatgpt.com/c/test")

    assert [message["id"] for message in result["messages"]] == ["later-created", "earlier-created"]
    assert result["attachments"] == payload["attachments"]


def test_mapping_with_missing_active_parent_is_marked_partial() -> None:
    payload = {
        "current_node": "leaf",
        "mapping": {
            "leaf": {
                "id": "leaf",
                "parent": "missing-parent",
                "message": {"id": "m1", "author": {"role": "assistant"}, "content": {"parts": ["text"]}},
            }
        },
    }
    result = ChatGPTBrowser._normalise_api_conversation(payload, "https://chatgpt.com/c/test")

    assert result["branch_truncated"] is True


def test_api_page_merge_preserves_order_and_requires_both_terminal_flags() -> None:
    latest = {
        "messages": [{"id": "m3"}, {"id": "m4"}],
        "page_info": {"has_previous_page": True, "has_next_page": False},
    }
    older = {
        "payload": {
            "messages": [{"id": "m1"}, {"id": "m2"}],
            "page_info": {"has_previous_page": False, "has_next_page": True},
        },
        "cursor_matches": True,
    }
    result = ChatGPTBrowser._combine_api_pages(latest, [older])

    assert [message["id"] for message in result["messages"]] == ["m1", "m2", "m3", "m4"]
    assert result["pagination"]["completeness"] == "verified"

    incomplete = ChatGPTBrowser._combine_api_pages(latest, [])
    assert incomplete["pagination"]["completeness"] == "partial"


def test_api_page_merge_marks_cursor_mismatch_partial() -> None:
    latest = {
        "messages": [{"id": "new"}],
        "page_info": {"has_previous_page": True, "has_next_page": False},
    }
    older = {
        "payload": {
            "messages": [{"id": "old"}],
            "page_info": {"has_previous_page": False, "has_next_page": True},
        },
        "cursor_matches": False,
    }
    result = ChatGPTBrowser._combine_api_pages(latest, [older])
    assert result["pagination"]["completeness"] == "partial"


def test_attachment_scan_count_is_only_a_minimum_coverage_gate() -> None:
    assert not _attachment_scan_reaches_api_count(
        {"stable": True, "observed_message_id_count": 8},
        63,
    )
    assert _attachment_scan_reaches_api_count(
        {"stable": True, "observed_message_id_count": 63},
        63,
    )
    assert not _attachment_scan_reaches_api_count(
        {"stable": True, "observed_message_id_count": True},
        1,
    )


def test_api_attachment_reference_summary_counts_keys_without_values() -> None:
    summary = _api_attachment_reference_summary(
        [
            {
                "rich_parts": [
                    {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_shared#full"},
                    {"content_type": "execution_output", "sandbox_path": "/private/path-marker"},
                ]
            }
        ],
        [
            {"library_file_id": "file_shared", "file_id": "file_shared"},
            {"file_id": "private-file-marker"},
        ],
    )

    assert summary == {
        "attachment_reference_count": 2,
        "attachment_file_id_count": 2,
        "attachment_sandbox_path_count": 0,
        "library_file_id_count": 1,
        "library_file_id_with_file_id_count": 1,
        "library_file_id_matching_file_id_count": 1,
        "library_file_id_pointer_overlap_count": 1,
        "file_id_pointer_overlap_count": 1,
        "pointer_reference_count": 1,
        "library_file_id_with_sandbox_path_count": 0,
        "rich_part_count": 2,
        "rich_part_file_id_count": 0,
        "rich_part_sandbox_path_count": 1,
        "rich_part_content_type_counts": {
            "execution_output": 1,
            "image_asset_pointer": 1,
        },
    }
    assert "private/path-marker" not in repr(summary)
    assert "private-file-marker" not in repr(summary)
    assert "file_shared" not in repr(summary)


def test_interpreter_metadata_resource_fallback_is_session_scoped_and_deduplicated() -> None:
    conversation_url = "https://chatgpt.com/c/test-id"
    first = (
        "https://chatgpt.com/backend-api/conversation/test-id/interpreter/download"
        "?message_id=message-1&sandbox_path=%2Fmnt%2Fdata%2Fresult.html"
    )
    resources = [
        first,
        (
            "https://chatgpt.com/backend-api/conversation/test-id/interpreter/download"
            "?sandbox_path=%2Fmnt%2Fdata%2Fresult.html&message_id=message-1"
        ),
        "https://attacker.example/backend-api/conversation/test-id/interpreter/download?message_id=m&sandbox_path=%2Fx",
        "https://chatgpt.com/backend-api/conversation/other/interpreter/download?message_id=m&sandbox_path=%2Fx",
        "https://chatgpt.com/backend-api/conversation/test-id/interpreter/download?message_id=m",
        (
            "https://chatgpt.com/backend-api/conversation/test-id/interpreter/download"
            "?message_id=m&sandbox_path=%2Fx&signature=must-not-be-forwarded"
        ),
    ]

    assert _interpreter_metadata_resource_urls(resources, conversation_url, "test-id") == [first]


def test_sandbox_attachment_references_are_message_scoped_and_deduplicated() -> None:
    messages = [
        {
            "id": "assistant-message",
            "role": "assistant",
            "text": (
                "[Report](sandbox:/mnt/data/report.html) "
                "[Archive](sandbox:/mnt/data/research.zip) "
                "[Again](sandbox:/mnt/data/report.html) "
                "sandbox:/mnt/data/../outside.zip"
            ),
        },
        {"id": "user-message", "role": "user", "text": "sandbox:/mnt/data/user-reference.png"},
    ]

    assert _sandbox_attachment_references(messages) == [
        ("assistant-message", "/mnt/data/report.html"),
        ("assistant-message", "/mnt/data/research.zip"),
    ]


def test_login_detection_requires_an_explicit_auth_route() -> None:
    assert _is_explicit_login_page("https://auth.openai.com/log-in") is True
    assert _is_explicit_login_page("https://chatgpt.com/auth/login?next=%2Fc%2Ftest") is True
    assert _is_explicit_login_page("https://chatgpt.com/c/test") is False
    assert _is_explicit_login_page("https://chatgpt.com/") is False


def test_successful_conversation_api_response_is_login_evidence_without_composer() -> None:
    assert _has_authenticated_conversation_response(
        [{"direction": "latest", "payload": {"messages": []}}]
    ) is True
    assert _has_authenticated_conversation_response(
        [{"direction": "older", "payload": {"messages": [{"id": "m"}]}}]
    ) is False
    assert _has_authenticated_conversation_response(
        [{"direction": "latest", "payload": None}]
    ) is False


def test_file_download_resource_fallback_requires_observed_shape_and_api_reference() -> None:
    conversation_url = "https://chatgpt.com/c/test-id"
    first = (
        "https://chatgpt.com/backend-api/files/download/file_123"
        "?download_intent=false&inline=false"
    )
    resources = [
        first,
        (
            "https://chatgpt.com/backend-api/files/download/file_123"
            "?inline=false&download_intent=false"
        ),
        "https://chatgpt.com/backend-api/files/download/file_unreferenced?download_intent=false&inline=false",
        "https://chatgpt.com/backend-api/files/download/file_123?download_intent=false&inline=true",
        "https://attacker.example/backend-api/files/download/file_123?download_intent=false&inline=false",
    ]

    assert _file_download_resource_urls(resources, conversation_url, {"file_123"}) == [first]


def test_api_attachment_metadata_requires_reference_and_signed_content_id_match() -> None:
    conversation_url = "https://chatgpt.com/c/test-id"
    source_url = (
        "https://chatgpt.com/backend-api/files/download/file_123"
        "?conversation_id=test-id&inline=true"
    )
    payload = {
        "status": "success",
        "file_name": "input.png",
        "file_size_bytes": 101,
        "download_url": (
            "https://chatgpt.com/backend-api/estuary/content"
            "?cid=conversation&id=file_123&p=0&sig=fake&ts=1&v=1"
        ),
    }

    assert _api_attachment_metadata_error(
        source_url, conversation_url, "file_123", "input.png", 101, payload
    ) is None
    assert _api_attachment_metadata_error(
        source_url, conversation_url, "file_123", "different.png", 101, payload
    ) == "reference_metadata_mismatch"
    assert _api_attachment_metadata_error(
        source_url, conversation_url, "file_123", "input.png", 102, payload
    ) == "reference_metadata_mismatch"
    mismatched_content_id = {
        **payload,
        "download_url": payload["download_url"].replace("id=file_123", "id=other"),
    }
    assert _api_attachment_metadata_error(
        source_url, conversation_url, "file_123", "input.png", 101, mismatched_content_id
    ) == "content_id_mismatch"
    assert _api_attachment_metadata_error(
        source_url.replace("inline=true", "inline=false"),
        conversation_url,
        "file_123",
        "input.png",
        101,
        payload,
    ) == "source_route_unverified"


def test_normalized_library_attachment_keeps_file_id_distinct_from_handle_identity() -> None:
    normalized = ChatGPTBrowser._normalise_api_conversation(
        {
            "messages": [{
                "id": "user-message",
                "author": {"role": "user"},
                "content": {"parts": []},
                "metadata": {
                    "attachments": [{
                        "id": "file-original-id",
                        "library_file_id": "library-id",
                        "name": "input.png",
                        "mime_type": "image/png",
                        "size": 512134,
                    }]
                },
            }]
        },
        "https://chatgpt.com/c/test-id",
    )

    attachment = normalized["attachments"][0]
    assert attachment["attachment_id"] == "library-id"
    assert attachment["file_id"] == "file-original-id"
    assert attachment["library_file_id"] == "library-id"


def test_observed_file_download_route_maps_to_matching_asset_pointer() -> None:
    source_url = (
        "https://chatgpt.com/backend-api/files/download/file_123"
        "?download_intent=false&inline=false"
    )
    records = _file_download_attachments_from_responses(
        [
            {
                "id": "assistant-image",
                "role": "assistant",
                "rich_parts": [
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": "sediment://file_123#thumbnail",
                        "size_bytes": 101,
                    }
                ],
            },
            {
                "id": "user-library-file",
                "role": "user",
                "attachments": [{
                    "attachment_id": "library-123",
                    "library_file_id": "library-123",
                    "filename": "source.png",
                    "mime_type": "image/png",
                    "size": 202,
                }],
            },
        ],
        [
            {
                "file_id": "file_123",
                "source_url": source_url,
                "status": "success",
                "has_download_url": True,
                "download_url": "/backend-api/estuary/content?id=file_123",
                "filename": "generated.png",
                "mime_type": "image/png",
                "size": 101,
            },
            {
                "file_id": "unmatched-file",
                "source_url": "https://chatgpt.com/backend-api/files/download/unmatched-file?download_intent=false&inline=false",
                "status": "success",
                "has_download_url": True,
                "download_url": "/backend-api/estuary/content?id=unmatched-file",
            },
        ],
    )

    assert records == [{
        "attachment_id": "file_123",
        "file_id": "file_123",
        "message_id": "assistant-image",
        "filename": "generated.png",
        "mime_type": "image/png",
        "size": None,
        "file_kind": "image_asset_pointer",
        "source_url": source_url,
        "downloadable": True,
        "download_candidate": False,
        "unavailable_reason": None,
    }]


def test_pointer_download_response_does_not_relabel_library_reference() -> None:
    source_url = (
        "https://chatgpt.com/backend-api/files/download/file_pointer"
        "?download_intent=false&inline=false"
    )
    records = _file_download_attachments_from_responses(
        [{
            "id": "user-image",
            "role": "user",
            "attachments": [{
                "attachment_id": "library-id",
                "id": "file_pointer",
                "file_id": "file_pointer",
                "library_file_id": "library-id",
                "filename": "input.png",
                "mime_type": "image/png",
                "size": 202,
            }],
            "rich_parts": [{
                "content_type": "image_asset_pointer",
                "asset_pointer": "sediment://file_pointer#thumbnail",
            }],
        }],
        [{
            "file_id": "file_pointer",
            "source_url": source_url,
            "status": "success",
            "has_download_url": True,
            "filename": "input.png",
            "mime_type": "image/png",
            "size": 101,
        }],
    )

    assert len(records) == 1
    assert records[0]["attachment_id"] == "file_pointer"
    assert records[0]["file_id"] == "file_pointer"
    assert records[0]["file_kind"] == "image_asset_pointer"
    assert records[0]["source_url"] == source_url


def test_older_page_collection_reaches_source_end_after_more_than_1000_pages() -> None:
    def payload(index: int, page_count: int) -> dict[str, object]:
        return {
            "messages": [{"id": f"message-{index}"}],
            "page_info": {
                "start_cursor": f"cursor-{index}",
                "has_previous_page": index < page_count - 1,
            },
        }

    async def run() -> None:
        page_count = 1002
        latest = payload(0, page_count)
        requests = 0

        async def fetch(current: dict[str, object]) -> dict[str, object]:
            nonlocal requests
            info = current["page_info"]
            index = int(str(info["start_cursor"]).removeprefix("cursor-")) + 1
            requests += 1
            return {"cursor_matches": True, "payload": payload(index, page_count)}

        pages, failed = await _collect_older_api_pages(latest, fetch)

        assert failed is False
        assert requests == page_count - 1
        assert len(pages) == page_count - 1
        assert pages[-1]["payload"]["page_info"]["has_previous_page"] is False

    asyncio.run(run())


def test_older_page_collection_stops_on_a_repeated_cursor() -> None:
    async def run() -> None:
        latest = {
            "messages": [{"id": "new"}],
            "page_info": {"start_cursor": "same", "has_previous_page": True},
        }

        async def fetch(_: dict[str, object]) -> dict[str, object]:
            return {"cursor_matches": True, "payload": latest}

        pages, failed = await _collect_older_api_pages(latest, fetch)
        assert failed is True
        assert len(pages) == 1

    asyncio.run(run())


def test_older_page_collection_consumes_payload_without_retaining_it() -> None:
    async def run() -> None:
        latest = {
            "messages": [{"id": "new"}],
            "page_info": {"start_cursor": "cursor-new", "has_previous_page": True, "has_next_page": False},
        }
        staged: list[dict[str, object]] = []

        async def fetch(_: dict[str, object]) -> dict[str, object]:
            return {
                "cursor_matches": True,
                "payload": {
                    "messages": [{"id": "old"}],
                    "page_info": {"start_cursor": "cursor-old", "has_previous_page": False, "has_next_page": True},
                },
            }

        async def consume(record: dict[str, object]) -> None:
            staged.append(record)

        pages, failed = await _collect_older_api_pages(latest, fetch, on_page=consume)

        assert failed is False
        assert len(staged) == 1
        assert len(staged[0]["payload"]["messages"]) == 1
        assert len(pages) == 1
        assert pages[0]["payload"]["messages"] == []
        assert pages[0]["payload"]["page_info"]["has_previous_page"] is False

    asyncio.run(run())


def test_file_card_download_uses_and_closes_a_temporary_tab(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False
            self.navigations: list[str] = []
            self.listeners: dict[str, list[object]] = {}

        def on(self, event: str, callback: object) -> None:
            self.listeners.setdefault(event, []).append(callback)

        def remove_listener(self, event: str, callback: object) -> None:
            self.listeners[event].remove(callback)

        async def goto(self, url: str, **_: object) -> None:
            self.navigations.append(url)
            self.url = url
            response = type(
                "FakeResponse",
                (),
                {"url": "https://chatgpt.com/backend-api/conversations/file-card", "status": 200},
            )()
            for callback in self.listeners.get("response", []):
                callback(response)

        async def wait_for_timeout(self, _: int) -> None:
            await asyncio.sleep(0)

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self, existing_page: FakePage, temp_page: FakePage) -> None:
            self.pages = [existing_page]
            self.temp_page = temp_page

        async def new_page(self) -> FakePage:
            return self.temp_page

    async def run() -> None:
        browser = ChatGPTBrowser(Settings.from_env(tmp_path))
        existing = FakePage("https://chatgpt.com/c/file-card")
        temporary = FakePage("about:blank")
        browser._context = FakeContext(existing, temporary)

        async def start() -> None:
            return None

        async def no_direct_source(
            page: FakePage,
            source_url: str,
            conversation_id: str,
            file_id: str,
        ) -> None:
            await page.goto(f"https://chatgpt.com/c/{conversation_id}")
            return None

        async def has_composer(_: FakePage) -> bool:
            return True

        async def load(page: FakePage) -> dict[str, object]:
            assert page is temporary
            return {"stable": True}

        async def download(page: FakePage, file_id: str, conversation_id: str) -> tuple[bytes, str, str]:
            assert page is temporary
            assert file_id == "file-1"
            assert conversation_id == "file-card"
            return b"file bytes", "application/octet-stream", "attachment"

        monkeypatch.setattr(browser, "start", start)
        monkeypatch.setattr(browser, "_new_page", browser._context.new_page)
        monkeypatch.setattr(browser, "_page_has_composer", has_composer)
        monkeypatch.setattr(browser, "_download_file_download_endpoint", no_direct_source)
        monkeypatch.setattr(browser, "_load_all_rendered_messages", load)
        monkeypatch.setattr(browser, "_download_file_card", download)
        result = await browser.download_attachment(
            "https://chatgpt.com/backend-api/files/download/file-1?download_intent=false&inline=false",
            attachment_id="file-1",
            conversation_url="https://chatgpt.com/c/file-card",
        )

        assert result == (b"file bytes", "application/octet-stream", "attachment")
        assert existing.navigations == []
        assert existing.closed is False
        assert temporary.closed is True

    asyncio.run(run())


def test_file_download_refreshes_metadata_with_page_context(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConversationResponse:
        status = 200

        class Request:
            async def all_headers(self) -> dict[str, str]:
                return {
                    "authorization": "Bearer test-only",
                    "oai-session-id": "session-test",
                    "x-openai-target-path": "/backend-api/conversations/conversation",
                    "x-openai-target-route": "/backend-api/conversations/conversation",
                }

        request = Request()
        url = "https://chatgpt.com/backend-api/conversations/conversation"

    class FakeContentResponse:
        ok = True
        headers = {"content-type": "image/png", "content-disposition": "attachment; filename=image.png"}

        async def body(self) -> bytes:
            return b"image-bytes"

    class FakePage:
        url = "about:blank"

        class Expectation:
            @property
            def value(self):
                async def result() -> FakeConversationResponse:
                    return FakeConversationResponse()
                return result()

            async def __aenter__(self) -> "FakePage.Expectation":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        def expect_response(self, *_: object, **__: object) -> "FakePage.Expectation":
            return self.Expectation()

        async def goto(self, url: str, **_: object) -> None:
            self.url = url

    class FakeRequest:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get(self, url: str, **_: object) -> FakeContentResponse:
            self.urls.append(url)
            return FakeContentResponse()

    async def run() -> None:
        browser = ChatGPTBrowser(Settings.from_env(tmp_path))
        request = FakeRequest()
        browser._context = type("Context", (), {"request": request})()
        page = FakePage()

        async def logged_in(_: FakePage) -> bool:
            return True

        async def refresh(_: FakePage, url: str, headers: dict[str, str]) -> dict[str, object]:
            assert url.startswith("https://chatgpt.com/backend-api/files/download/file_123?")
            assert headers["x-openai-target-path"] == "/backend-api/files/download/file_123"
            assert headers["x-openai-target-route"] == "/backend-api/files/download/file_123"
            return {
                "status": 200,
                "payload": {
                    "status": "success",
                    "download_url": "/backend-api/estuary/content?id=file_123",
                    "metadata": {},
                },
                "attempts": [{"status": 200}],
            }

        monkeypatch.setattr(browser, "_page_has_composer", logged_in)
        monkeypatch.setattr("chatgpt_oracle.browser._authenticated_page_json_get", refresh)
        result = await browser._download_file_download_endpoint(
            page,
            "https://chatgpt.com/backend-api/files/download/file_123?conversation_id=conversation&inline=true",
            "conversation",
            "file_123",
        )

        assert result == (b"image-bytes", "image/png", "attachment; filename=image.png")
        assert request.urls == ["https://chatgpt.com/backend-api/estuary/content?id=file_123"]

    asyncio.run(run())
