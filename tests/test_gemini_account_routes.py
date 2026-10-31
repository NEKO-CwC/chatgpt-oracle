from __future__ import annotations

import pytest

from chatgpt_oracle.providers import (
    CHATGPT,
    GEMINI,
    gemini_account_home,
    gemini_account_index,
    gemini_same_account,
)


@pytest.mark.parametrize("prefix", ["", "u/0/", "u/1/", "u/12/"])
def test_canonical_conversation_preserves_account_prefix(prefix):
    url = f"https://gemini.google.com/{prefix}app/524fda7beb092799"
    assert GEMINI.canonical_url(f"{url}/?pli=1#response") == url


def test_legacy_urls_stay_legacy_but_compare_as_default_account():
    legacy = "https://gemini.google.com/app/legacy-turn"
    explicit = "https://gemini.google.com/u/0/app/legacy-turn"
    assert GEMINI.canonical_url(legacy) == legacy
    assert GEMINI.canonical_url(explicit) == explicit
    assert gemini_same_account(legacy, explicit)
    assert not gemini_same_account(legacy, "https://gemini.google.com/u/1/app?pli=1")


@pytest.mark.parametrize("index", [0, 1, 12])
def test_explicit_account_home_round_trip(index):
    home = gemini_account_home(index)
    assert home == f"https://gemini.google.com/u/{index}/app?pli=1"
    assert gemini_account_index(home) == index
    with pytest.raises(ValueError):
        GEMINI.canonical_url(home)


@pytest.mark.parametrize("index", [-1, True, False, 1.0, "1", None])
def test_account_home_rejects_invalid_indices(index):
    with pytest.raises(ValueError):
        gemini_account_home(index)


@pytest.mark.parametrize("url", [
    "http://gemini.google.com/u/1/app/a",
    "https://gemini.google.com.evil.test/u/1/app/a",
    "https://gemini.google.com@evil.test/u/1/app/a",
    "https://evil.test@gemini.google.com/u/1/app/a",
    "https://@gemini.google.com/u/1/app/a",
    "https://gemini.google.com:/u/1/app/a",
    "https://gemini.google.com:443/u/1/app/a",
    "https://gemini.google.com/u/-1/app/a",
    "https://gemini.google.com/u/+1/app/a",
    "https://gemini.google.com/u/01/app/a",
    "https://gemini.google.com/u/one/app/a",
    "https://gemini.google.com/u/1/app/a/b",
    "https://gemini.google.com/u/1/app/a%2fb",
    "https://gemini.google.com/u/1/app/%61",
    "https://gemini.google.com/u/1/app/..",
    "https://gemini.google.com/u/1/app/a;params",
    "https://gemini.google.com//u/1/app/a",
    "https://gemini.google.com/u/1//app/a",
    "https://gemini.google.com/u/1/app/a//",
    "https://gemini.google.com/u/1/app/a\\b",
    "https://gemini.google.com/u/1/app/a\nb",
    "https://gemini.google.com/u/1/app/a\tb",
    "https://gemini.google.com/u/1/app/a b",
])
def test_malformed_and_misleading_routes_are_rejected(url):
    with pytest.raises(ValueError):
        GEMINI.canonical_url(url)
    with pytest.raises(ValueError):
        gemini_account_index(url)


@pytest.mark.parametrize("url", [
    "https://gemini.google.com/app",
    "https://gemini.google.com/app/",
    "https://gemini.google.com/u/0/app?pli=1",
    "https://gemini.google.com/u/1/app/?pli=1",
])
def test_homes_are_valid_account_routes_but_not_conversations(url):
    assert gemini_account_index(url) in {0, 1}
    with pytest.raises(ValueError):
        GEMINI.canonical_url(url)


def test_chatgpt_url_behavior_is_preserved():
    assert CHATGPT.canonical_url("https://chatgpt.com/c/abc/?x=1#foo") == "https://chatgpt.com/c/abc"
