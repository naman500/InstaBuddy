"""Instagram URL validation and shortcode extraction.

Validation happens before any network request, so a typo costs nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from utils.errors import ValidationError

#: URL path segments that identify a single piece of Instagram content.
SUPPORTED_PATH_TYPES = ("p", "reel", "reels", "tv")

#: Hosts we accept. Anything else is rejected outright.
_ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "instagr.am",
    "www.instagr.am",
}

#: Instagram shortcodes are base64-ish: letters, digits, hyphen, underscore.
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")

_PATH_RE = re.compile(
    r"^/(?:[A-Za-z0-9_.]+/)?(?P<kind>p|reel|reels|tv)/(?P<shortcode>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

#: First path segments that are Instagram features, not usernames. A profile URL
#: like ``instagram.com/<username>/`` must not match any of these.
_RESERVED_PROFILE_SEGMENTS = {
    "p",
    "reel",
    "reels",
    "tv",
    "explore",
    "accounts",
    "stories",
    "direct",
    "about",
    "developer",
    "legal",
    "privacy",
    "api",
    "web",
    "emails",
    "challenge",
    "oauth",
    "session",
    "graphql",
    "ajax",
}

#: Instagram usernames: letters, digits, dots and underscores, up to 30 chars.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{1,30}$")

#: A bare profile path: a single username segment, optional trailing slash.
_PROFILE_RE = re.compile(r"^/(?P<username>[A-Za-z0-9_.]+)/?$")


def _normalize(url: str) -> str:
    """Add a scheme if the user pasted a bare host."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned):
        cleaned = "https://" + cleaned
    return cleaned


def _parse(url: str) -> re.Match[str] | None:
    """Return the path match for a well-formed Instagram content URL."""
    normalized = _normalize(url)
    if not normalized:
        return None

    try:
        parsed = urlparse(normalized)
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None

    return _PATH_RE.match(parsed.path or "")


def is_valid_instagram_url(url: str) -> bool:
    """True if ``url`` points at a single Instagram post, reel or video.

    Accepts ``/p/``, ``/reel/``, ``/reels/`` and ``/tv/`` paths, with or without
    a leading username segment, and tolerates query strings and missing scheme.
    """
    match = _parse(url)
    if match is None:
        return False
    return bool(_SHORTCODE_RE.match(match.group("shortcode")))


def extract_instagram_shortcode(url: str) -> str:
    """Return the shortcode from an Instagram URL.

    Raises:
        ValidationError: if the URL is not a supported Instagram content URL.
    """
    match = _parse(url)
    if match is None:
        raise ValidationError(f"Unsupported Instagram URL: {url!r}")

    shortcode = match.group("shortcode")
    if not _SHORTCODE_RE.match(shortcode):
        raise ValidationError(f"Malformed Instagram shortcode in URL: {url!r}")

    return shortcode


def extract_url_type(url: str) -> str:
    """Return the URL path type (``p``, ``reel``, ``reels`` or ``tv``).

    This is a *hint* only. The real content type comes from inspecting the
    Instaloader ``Post`` object, because a ``/reel/`` URL does not guarantee the
    content is a Reel.
    """
    match = _parse(url)
    if match is None:
        raise ValidationError(f"Unsupported Instagram URL: {url!r}")
    return match.group("kind").lower()


def canonical_post_url(shortcode: str) -> str:
    """Build a canonical ``/p/<shortcode>/`` URL."""
    if not _SHORTCODE_RE.match(shortcode or ""):
        raise ValidationError(f"Invalid shortcode: {shortcode!r}")
    return f"https://www.instagram.com/p/{shortcode}/"


def validate_instagram_url(url: str) -> str:
    """Validate and return the shortcode, raising a friendly error if invalid."""
    if not is_valid_instagram_url(url):
        raise ValidationError(f"Rejected URL: {url!r}")
    return extract_instagram_shortcode(url)


# ---------------------------------------------------------------------------
# Profile URLs
# ---------------------------------------------------------------------------
# A profile URL points at a whole account (``instagram.com/<username>/``) rather
# than a single post. It is handled by the batch/profile download flow, which
# lists the account's posts and runs the per-post pipeline for each one.


def _profile_match(url: str) -> re.Match[str] | None:
    """Return the username match for a bare Instagram profile URL.

    Returns ``None`` for anything that is not a single-segment profile path, or
    whose first segment is a reserved Instagram feature (``/explore/`` etc.).
    A single-post content URL also returns ``None`` here, so the two URL shapes
    never overlap.
    """
    normalized = _normalize(url)
    if not normalized:
        return None

    try:
        parsed = urlparse(normalized)
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None

    match = _PROFILE_RE.match(parsed.path or "")
    if match is None:
        return None

    username = match.group("username")
    if username.lower() in _RESERVED_PROFILE_SEGMENTS:
        return None
    if not _USERNAME_RE.match(username):
        return None

    return match


def is_profile_url(url: str) -> bool:
    """True if ``url`` points at an Instagram profile (whole account).

    A single post, reel or video URL returns ``False`` so callers can route the
    two shapes to different flows.
    """
    if is_valid_instagram_url(url):
        return False
    return _profile_match(url) is not None


def extract_username(url: str) -> str:
    """Return the account username from an Instagram profile URL.

    Raises:
        ValidationError: if the URL is not a supported profile URL.
    """
    match = _profile_match(url)
    if match is None:
        raise ValidationError(f"Unsupported Instagram profile URL: {url!r}")
    return match.group("username")


def canonical_profile_url(username: str) -> str:
    """Build a canonical ``/<username>/`` profile URL."""
    if not _USERNAME_RE.match(username or ""):
        raise ValidationError(f"Invalid Instagram username: {username!r}")
    return f"https://www.instagram.com/{username}/"


# ---------------------------------------------------------------------------
# URL lists
# ---------------------------------------------------------------------------
# A pasted or uploaded list of post/reel links for batch download. Parsing is
# entirely local: nothing here contacts Instagram.

#: Separators between URLs: newlines, commas, semicolons, tabs and spaces.
_LIST_SPLIT_RE = re.compile(r"[\s,;]+")

#: Hard cap on entries per list, to keep a single run reasonable.
MAX_URL_LIST_ENTRIES = 500


@dataclass
class UrlListEntry:
    shortcode: str
    url: str


@dataclass
class UrlListParseResult:
    """Outcome of parsing a URL list."""

    entries: list[UrlListEntry] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    profile_links: list[str] = field(default_factory=list)
    duplicates: int = 0
    truncated: int = 0

    @property
    def shortcodes(self) -> list[str]:
        return [entry.shortcode for entry in self.entries]


def parse_url_list(
    text: str, *, max_entries: int = MAX_URL_LIST_ENTRIES
) -> UrlListParseResult:
    """Split text into Instagram post URLs, deduplicated by shortcode.

    Accepts one URL per line, or URLs separated by commas, semicolons or
    spaces, so a pasted list or a simple CSV column both work. Lines starting
    with ``#`` are comments. Order is preserved; the first occurrence of a
    shortcode wins, so ``/reel/X`` and ``/p/X`` count as one post.
    """
    result = UrlListParseResult()
    seen: set[str] = set()

    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        for token in _LIST_SPLIT_RE.split(line):
            token = token.strip().strip("\"'<>()[]")
            if not token:
                continue

            if not is_valid_instagram_url(token):
                if is_profile_url(token):
                    result.profile_links.append(token)
                elif "instagram" in token.lower() or "instagr.am" in token.lower():
                    result.invalid.append(token)
                # Anything else (CSV headers, stray words) is ignored silently.
                continue

            shortcode = extract_instagram_shortcode(token)
            if shortcode in seen:
                result.duplicates += 1
                continue
            if len(result.entries) >= max_entries:
                result.truncated += 1
                continue

            seen.add(shortcode)
            result.entries.append(UrlListEntry(shortcode=shortcode, url=token))

    return result