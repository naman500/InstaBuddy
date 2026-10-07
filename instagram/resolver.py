"""Turn an Instagram URL into a validated :class:`InstagramPost`.

This is the *analyze* phase. It performs exactly one content fetch and writes
nothing to disk, so the user can see what a post contains and choose what to
download before any media transfers.

Content type comes from the Instaloader ``Post`` object, not from the URL. A
``/reel/`` link pointing at a photo resolves to ``IMAGE``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from models.instagram import InstagramMediaItem, InstagramPost
from models.media import ContentType, MediaType
from utils.errors import InstagramError
from utils.paths import carousel_filename
from utils.validation import (
    canonical_post_url,
    extract_instagram_shortcode,
    extract_url_type,
    extract_username,
)

logger = logging.getLogger(__name__)

TYPENAME_IMAGE = "GraphImage"
TYPENAME_VIDEO = "GraphVideo"
TYPENAME_SIDECAR = "GraphSidecar"

#: Instagram's own product classification, when we can see it.
_REEL_PRODUCT_TYPES = {"clips"}
_NON_REEL_PRODUCT_TYPES = {"feed", "igtv"}

#: URL hints that suggest a Reel, used only once the post is known to be video.
_REEL_URL_HINTS = {"reel", "reels"}

IMAGE_FILENAME = "image.jpg"
VIDEO_FILENAME = "video.mp4"
COVER_FILENAME = "cover.jpg"


def _raw_product_type(post: Any) -> str | None:
    """Read ``product_type`` from the raw node, if present.

    Instaloader does not expose this publicly. It is the most reliable Reel
    signal when available, so we read it defensively and treat absence as
    normal rather than exceptional.
    """
    node = getattr(post, "_node", None)
    if not isinstance(node, dict):
        return None
    value = node.get("product_type")
    return str(value).lower() if value else None


def _coerce_datetime(value: Any) -> datetime:
    """Normalise Instaloader's timestamp into an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def detect_content_type(post: Any, url_hint: str | None = None) -> ContentType:
    """Classify a post.

    The typename is authoritative for image / video / carousel. The URL hint
    only ever refines an already-confirmed video into ``REEL`` versus
    ``VIDEO``; it can never turn a photo into a Reel.
    """
    typename = getattr(post, "typename", None)

    if typename == TYPENAME_SIDECAR:
        return ContentType.CAROUSEL

    if typename == TYPENAME_IMAGE:
        return ContentType.IMAGE

    is_video = bool(getattr(post, "is_video", False))

    if typename == TYPENAME_VIDEO or is_video:
        product_type = _raw_product_type(post)
        if product_type in _REEL_PRODUCT_TYPES:
            return ContentType.REEL
        if product_type in _NON_REEL_PRODUCT_TYPES:
            return ContentType.VIDEO
        # No product_type from Instagram: fall back to the link the user pasted.
        if (url_hint or "").lower() in _REEL_URL_HINTS:
            return ContentType.REEL
        return ContentType.VIDEO

    raise InstagramError(
        f"Unsupported Instagram post type: {typename!r}",
        user_message=(
            "This kind of Instagram content is not supported. Please use a post, "
            "reel or video link."
        ),
    )


def _image_items(post: Any) -> list[InstagramMediaItem]:
    url = getattr(post, "url", None)
    if not url:
        raise InstagramError(
            "Post has no image URL",
            user_message="Instagram did not provide an image for this post.",
        )
    return [
        InstagramMediaItem(
            index=1,
            media_type=MediaType.IMAGE,
            source_url=url,
            filename=IMAGE_FILENAME,
        )
    ]


def _video_items(post: Any) -> list[InstagramMediaItem]:
    """Video plus its cover image.

    For V1 the "image" of a Reel is its cover/thumbnail. Frame extraction is a
    later feature and is not attempted here.
    """
    video_url = getattr(post, "video_url", None)
    if not video_url:
        raise InstagramError(
            "Post has no video URL",
            user_message=(
                "Instagram did not provide a video file for this post. It may "
                "require an authenticated session."
            ),
        )

    items = [
        InstagramMediaItem(
            index=1,
            media_type=MediaType.VIDEO,
            source_url=video_url,
            filename=VIDEO_FILENAME,
            is_video=True,
        )
    ]

    cover_url = getattr(post, "url", None)
    if cover_url:
        items.append(
            InstagramMediaItem(
                index=2,
                media_type=MediaType.IMAGE,
                source_url=cover_url,
                filename=COVER_FILENAME,
                is_cover=True,
            )
        )
    return items


def _carousel_items(post: Any) -> list[InstagramMediaItem]:
    """One item per sidecar child, preserving Instagram's ordering."""
    items: list[InstagramMediaItem] = []

    try:
        nodes = list(post.get_sidecar_nodes())
    except Exception as exc:
        raise InstagramError(
            f"Could not read carousel children: {exc}",
            user_message="The items in this carousel post could not be listed.",
        ) from exc

    for index, node in enumerate(nodes, start=1):
        is_video = bool(getattr(node, "is_video", False))
        media_type = MediaType.VIDEO if is_video else MediaType.IMAGE
        source_url = (
            getattr(node, "video_url", None)
            if is_video
            else getattr(node, "display_url", None)
        )
        if not source_url:
            logger.warning("Skipping carousel item %d with no usable URL", index)
            continue

        items.append(
            InstagramMediaItem(
                index=index,
                media_type=media_type,
                source_url=source_url,
                filename=carousel_filename(index, media_type),
                is_video=is_video,
            )
        )

    if not items:
        raise InstagramError(
            "Carousel contained no usable media",
            user_message="No downloadable media was found in this carousel post.",
        )
    return items


def build_media_items(post: Any, content_type: ContentType) -> list[InstagramMediaItem]:
    """Build the ordered media list for a resolved content type."""
    if content_type is ContentType.CAROUSEL:
        return _carousel_items(post)
    if content_type in (ContentType.REEL, ContentType.VIDEO):
        return _video_items(post)
    return _image_items(post)


def build_post_model(
    post: Any,
    *,
    url: str,
    content_type: ContentType | None = None,
    url_hint: str | None = None,
) -> InstagramPost:
    """Assemble an :class:`InstagramPost` from an Instaloader ``Post``."""
    resolved_type = content_type or detect_content_type(post, url_hint)
    media_items = build_media_items(post, resolved_type)

    shortcode = getattr(post, "shortcode", "") or ""
    media_id = getattr(post, "mediaid", None)

    model = InstagramPost(
        id=str(media_id) if media_id is not None else shortcode,
        shortcode=shortcode,
        url=url,
        username=getattr(post, "owner_username", "") or "unknown",
        caption=getattr(post, "caption", None),
        created_at=_coerce_datetime(getattr(post, "date_utc", None)),
        content_type=resolved_type,
        is_video=bool(getattr(post, "is_video", False)),
        is_carousel=resolved_type is ContentType.CAROUSEL,
        media_items=media_items,
    )

    logger.info(
        "Resolved shortcode=%s type=%s author=%s media_items=%d",
        model.shortcode,
        model.content_type.value,
        model.username,
        len(model.media_items),
    )
    return model


def resolve_post(url: str, client: Any) -> InstagramPost:
    """Validate ``url``, fetch the post and return the analyzed model.

    Args:
        url: An Instagram post, reel or video URL.
        client: An :class:`instagram.client.InstagramClient`.

    Raises:
        ValidationError: if the URL is not a supported Instagram content URL.
        InstagramError: if the content cannot be retrieved or understood.
    """
    shortcode = extract_instagram_shortcode(url)
    url_hint = extract_url_type(url)

    logger.info("Analyzing Instagram URL shortcode=%s hint=%s", shortcode, url_hint)
    post = client.fetch_post(shortcode)

    return build_post_model(post, url=url, url_hint=url_hint)


def resolve_profile_posts(
    url: str, client: Any, *, limit: int | None = None
) -> tuple[str, list[Any]]:
    """Validate a profile URL and list its posts, newest first.

    Returns ``(username, posts)`` where each post is an Instaloader ``Post``
    from the listing. Build models from them with :func:`build_post_model`,
    which avoids a second lookup per post.

    Raises:
        ValidationError: if the URL is not a supported profile URL.
        InstagramError: if the profile cannot be listed.
    """
    username = extract_username(url)
    logger.info("Analyzing Instagram profile=%s limit=%s", username, limit)
    posts = client.fetch_profile_posts(username, limit=limit)
    return username, posts


def resolve_profile_shortcodes(
    url: str, client: Any, *, limit: int | None = None
) -> tuple[str, list[str]]:
    """Validate a profile URL and list its post shortcodes, newest first.

    This is the profile equivalent of the single-post *analyze* phase: it
    reaches Instagram only to list the account's posts, and downloads nothing.
    Each returned shortcode is resolved and downloaded individually by the
    ingestion service, reusing the same per-post pipeline as a single URL.

    Args:
        url: An Instagram profile URL, e.g. ``https://instagram.com/username/``.
        client: An :class:`instagram.client.InstagramClient`.
        limit: Maximum number of shortcodes to return; ``None`` means all.

    Returns:
        A ``(username, shortcodes)`` tuple.

    Raises:
        ValidationError: if the URL is not a supported profile URL.
        InstagramError: if the profile cannot be listed.
    """
    username = extract_username(url)
    logger.info("Analyzing Instagram profile=%s limit=%s", username, limit)
    shortcodes = client.fetch_profile_shortcodes(username, limit=limit)
    return username, shortcodes


def post_url_for_shortcode(shortcode: str) -> str:
    """Canonical single-post URL for a shortcode discovered via a profile."""
    return canonical_post_url(shortcode)
