"""URL validation and shortcode extraction."""

from __future__ import annotations

import pytest

from utils.errors import ValidationError
from utils.validation import (
    canonical_post_url,
    extract_instagram_shortcode,
    extract_url_type,
    is_valid_instagram_url,
    validate_instagram_url,
)

VALID_URLS = [
    ("https://www.instagram.com/p/ABC123/", "ABC123", "p"),
    ("https://www.instagram.com/reel/ABC123/", "ABC123", "reel"),
    ("https://www.instagram.com/reels/ABC123/", "ABC123", "reels"),
    ("https://www.instagram.com/tv/ABC123/", "ABC123", "tv"),
    ("https://instagram.com/p/ABC123", "ABC123", "p"),
    ("http://www.instagram.com/p/ABC123/", "ABC123", "p"),
    ("www.instagram.com/p/ABC123/", "ABC123", "p"),
    ("instagram.com/reel/ABC123/", "ABC123", "reel"),
    ("https://m.instagram.com/p/ABC123/", "ABC123", "p"),
    # Username-prefixed reel URLs, as shared from the mobile app
    ("https://www.instagram.com/creator.name/reel/ABC123/", "ABC123", "reel"),
    # Query strings and fragments are tolerated
    ("https://www.instagram.com/p/ABC123/?igshid=xyz", "ABC123", "p"),
    ("https://www.instagram.com/reel/ABC123/?utm_source=ig_web", "ABC123", "reel"),
    # Realistic shortcode shapes
    ("https://www.instagram.com/p/Cx-1_abcDEF/", "Cx-1_abcDEF", "p"),
]

INVALID_URLS = [
    "",
    "   ",
    "not a url",
    "https://example.com/p/ABC123/",
    "https://instagram.com.evil.com/p/ABC123/",
    "https://www.instagram.com/",
    "https://www.instagram.com/someuser/",
    "https://www.instagram.com/stories/user/123/",
    "https://www.instagram.com/explore/tags/python/",
    "https://www.instagram.com/p/",
    "https://www.facebook.com/reel/ABC123/",
    "ftp://www.instagram.com/p/ABC123/",
    "javascript:alert(1)",
    # Shortcode too short to be real
    "https://www.instagram.com/p/AB/",
]


@pytest.mark.parametrize("url,shortcode,kind", VALID_URLS)
def test_valid_urls_are_accepted(url: str, shortcode: str, kind: str) -> None:
    assert is_valid_instagram_url(url) is True
    assert extract_instagram_shortcode(url) == shortcode
    assert extract_url_type(url) == kind


@pytest.mark.parametrize("url", INVALID_URLS)
def test_invalid_urls_are_rejected(url: str) -> None:
    assert is_valid_instagram_url(url) is False


@pytest.mark.parametrize("url", INVALID_URLS)
def test_extract_raises_for_invalid_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        extract_instagram_shortcode(url)


def test_validate_returns_shortcode() -> None:
    assert validate_instagram_url("https://www.instagram.com/reel/XYZ789/") == "XYZ789"


def test_validate_raises_friendly_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_instagram_url("https://example.com/foo")
    assert "valid Instagram post" in exc_info.value.user_message


def test_url_type_is_a_hint_not_a_verdict() -> None:
    """A /reel/ URL only hints at a Reel; the resolver decides the real type."""
    assert extract_url_type("https://www.instagram.com/reel/ABC123/") == "reel"
    assert extract_url_type("https://www.instagram.com/p/ABC123/") == "p"


def test_canonical_url_round_trips() -> None:
    url = canonical_post_url("ABC123")
    assert url == "https://www.instagram.com/p/ABC123/"
    assert extract_instagram_shortcode(url) == "ABC123"


def test_canonical_url_rejects_bad_shortcode() -> None:
    with pytest.raises(ValidationError):
        canonical_post_url("no spaces allowed")


def test_none_is_handled() -> None:
    assert is_valid_instagram_url(None) is False  # type: ignore[arg-type]
