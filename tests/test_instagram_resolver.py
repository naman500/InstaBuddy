"""Content type detection, media item construction and post resolution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from instagram.resolver import (
    build_media_items,
    build_post_model,
    detect_content_type,
    resolve_post,
)
from models.media import ContentType, MediaSelection, MediaType
from utils.errors import InstagramError, ValidationError
from tests.conftest import FakeClient, FakePost, FakeSidecarNode

POST_URL = "https://www.instagram.com/p/ABC123/"
REEL_URL = "https://www.instagram.com/reel/ABC123/"
TV_URL = "https://www.instagram.com/tv/ABC123/"


# --------------------------------------------------------- content type detection


def test_image_post_detected(image_post: FakePost) -> None:
    assert detect_content_type(image_post, "p") is ContentType.IMAGE


def test_sidecar_detected_as_carousel(mixed_carousel_post: FakePost) -> None:
    assert detect_content_type(mixed_carousel_post, "p") is ContentType.CAROUSEL


def test_video_post_from_p_url_is_video(video_post: FakePost) -> None:
    assert detect_content_type(video_post, "p") is ContentType.VIDEO


def test_video_post_from_reel_url_is_reel(video_post: FakePost) -> None:
    """With no product_type available, the pasted link is the only signal."""
    assert detect_content_type(video_post, "reel") is ContentType.REEL
    assert detect_content_type(video_post, "reels") is ContentType.REEL


def test_tv_url_is_video_not_reel(video_post: FakePost) -> None:
    assert detect_content_type(video_post, "tv") is ContentType.VIDEO


def test_product_type_clips_wins_over_p_url(reel_post: FakePost) -> None:
    """Instagram's own classification beats the URL shape."""
    assert detect_content_type(reel_post, "p") is ContentType.REEL


def test_product_type_feed_overrides_reel_url() -> None:
    post = FakePost(
        typename="GraphVideo",
        is_video=True,
        video_url="https://cdn.example/v.mp4",
        product_type="feed",
    )
    assert detect_content_type(post, "reel") is ContentType.VIDEO


def test_reel_url_pointing_at_an_image_resolves_to_image(image_post: FakePost) -> None:
    """The headline requirement: a /reel/ link does not make content a Reel."""
    assert detect_content_type(image_post, "reel") is ContentType.IMAGE

    model = build_post_model(image_post, url=REEL_URL, url_hint="reel")
    assert model.content_type is ContentType.IMAGE
    assert model.is_carousel is False
    assert [i.media_type for i in model.media_items] == [MediaType.IMAGE]


def test_is_video_flag_without_typename_still_detected() -> None:
    post = FakePost(
        typename="SomethingNew",
        is_video=True,
        video_url="https://cdn.example/v.mp4",
    )
    assert detect_content_type(post, "reel") is ContentType.REEL


def test_unsupported_typename_raises() -> None:
    post = FakePost(typename="GraphStory", is_video=False)
    with pytest.raises(InstagramError) as exc_info:
        detect_content_type(post, "p")
    assert "not supported" in exc_info.value.user_message


# ------------------------------------------------------------------ media items


def test_image_post_has_single_item(image_post: FakePost) -> None:
    items = build_media_items(image_post, ContentType.IMAGE)
    assert len(items) == 1
    assert items[0].filename == "image.jpg"
    assert items[0].media_type is MediaType.IMAGE
    assert items[0].is_cover is False


def test_image_post_without_url_raises() -> None:
    with pytest.raises(InstagramError):
        build_media_items(FakePost(url=None), ContentType.IMAGE)


def test_video_post_yields_video_and_cover(video_post: FakePost) -> None:
    items = build_media_items(video_post, ContentType.REEL)
    assert [i.filename for i in items] == ["video.mp4", "cover.jpg"]
    assert items[0].is_video is True
    assert items[1].is_cover is True
    assert items[1].media_type is MediaType.IMAGE


def test_video_without_video_url_raises() -> None:
    post = FakePost(typename="GraphVideo", is_video=True, video_url=None)
    with pytest.raises(InstagramError) as exc_info:
        build_media_items(post, ContentType.REEL)
    assert "did not provide a video" in exc_info.value.user_message


def test_video_without_cover_still_works() -> None:
    post = FakePost(
        typename="GraphVideo",
        is_video=True,
        video_url="https://cdn.example/v.mp4",
        url=None,
    )
    items = build_media_items(post, ContentType.REEL)
    assert [i.filename for i in items] == ["video.mp4"]


def test_carousel_preserves_ordering(mixed_carousel_post: FakePost) -> None:
    items = build_media_items(mixed_carousel_post, ContentType.CAROUSEL)
    assert [i.filename for i in items] == ["001.jpg", "002.mp4", "003.jpg", "004.mp4"]
    assert [i.index for i in items] == [1, 2, 3, 4]
    assert [i.is_video for i in items] == [False, True, False, True]


def test_carousel_uses_video_url_for_video_children(
    mixed_carousel_post: FakePost,
) -> None:
    items = build_media_items(mixed_carousel_post, ContentType.CAROUSEL)
    assert items[1].source_url == "https://cdn.example/2.mp4"
    assert items[0].source_url == "https://cdn.example/1.jpg"


def test_carousel_skips_items_without_url() -> None:
    post = FakePost(
        typename="GraphSidecar",
        sidecar_nodes=[
            FakeSidecarNode(is_video=False, display_url="https://cdn.example/1.jpg"),
            FakeSidecarNode(is_video=False, display_url=None),
        ],
    )
    items = build_media_items(post, ContentType.CAROUSEL)
    assert len(items) == 1


def test_empty_carousel_raises() -> None:
    post = FakePost(typename="GraphSidecar", sidecar_nodes=[])
    with pytest.raises(InstagramError):
        build_media_items(post, ContentType.CAROUSEL)


def test_carousel_fetch_failure_is_wrapped() -> None:
    post = FakePost(
        typename="GraphSidecar", sidecar_raises=RuntimeError("network blip")
    )
    with pytest.raises(InstagramError) as exc_info:
        build_media_items(post, ContentType.CAROUSEL)
    assert "carousel" in exc_info.value.user_message.lower()


# ----------------------------------------------------------------- post model


def test_post_model_fields(reel_post: FakePost) -> None:
    model = build_post_model(reel_post, url=REEL_URL, url_hint="reel")
    assert model.shortcode == "ABC123"
    assert model.id == "123456789"
    assert model.username == "example_user"
    assert model.caption == "An example caption"
    assert model.url == REEL_URL
    assert model.content_type is ContentType.REEL
    assert model.created_at == datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def test_naive_timestamp_becomes_utc_aware() -> None:
    post = FakePost(date_utc=datetime(2026, 9, 21, 10, 0, 0))
    model = build_post_model(post, url=POST_URL, url_hint="p")
    assert model.created_at.tzinfo is not None


def test_missing_timestamp_falls_back_to_now() -> None:
    post = FakePost(date_utc=None)
    model = build_post_model(post, url=POST_URL, url_hint="p")
    assert model.created_at.tzinfo is not None


def test_missing_caption_is_allowed() -> None:
    model = build_post_model(FakePost(caption=None), url=POST_URL, url_hint="p")
    assert model.caption is None


def test_missing_username_falls_back() -> None:
    model = build_post_model(FakePost(owner_username=""), url=POST_URL, url_hint="p")
    assert model.username == "unknown"


def test_id_falls_back_to_shortcode_when_mediaid_missing() -> None:
    model = build_post_model(FakePost(mediaid=None), url=POST_URL, url_hint="p")
    assert model.id == "ABC123"


def test_counts(mixed_carousel_post: FakePost) -> None:
    model = build_post_model(mixed_carousel_post, url=POST_URL, url_hint="p")
    assert model.media_count == 4
    assert model.image_count == 2
    assert model.video_count == 2
    assert model.has_cover is False


def test_reel_counts_exclude_cover(reel_post: FakePost) -> None:
    model = build_post_model(reel_post, url=REEL_URL, url_hint="reel")
    assert model.has_cover is True
    assert model.media_count == 1, "cover must not inflate the media count"
    assert model.video_count == 1
    assert model.image_count == 0


# ------------------------------------------------------- selections and filtering


def test_image_post_never_offers_video(image_post: FakePost) -> None:
    model = build_post_model(image_post, url=POST_URL, url_hint="p")
    assert model.available_selections() == [MediaSelection.IMAGE]
    assert model.default_selection() is MediaSelection.IMAGE


def test_reel_offers_video_cover_and_both(reel_post: FakePost) -> None:
    model = build_post_model(reel_post, url=REEL_URL, url_hint="reel")
    assert model.available_selections() == [
        MediaSelection.VIDEO,
        MediaSelection.COVER,
        MediaSelection.VIDEO_AND_COVER,
    ]
    assert model.default_selection() is MediaSelection.VIDEO


def test_mixed_carousel_offers_all_three(mixed_carousel_post: FakePost) -> None:
    model = build_post_model(mixed_carousel_post, url=POST_URL, url_hint="p")
    assert model.available_selections() == [
        MediaSelection.IMAGES,
        MediaSelection.VIDEOS,
        MediaSelection.IMAGES_AND_VIDEOS,
    ]
    assert model.default_selection() is MediaSelection.IMAGES_AND_VIDEOS


def test_image_only_carousel_offers_images_only(
    image_carousel_post: FakePost,
) -> None:
    model = build_post_model(image_carousel_post, url=POST_URL, url_hint="p")
    assert model.available_selections() == [MediaSelection.IMAGES]
    assert model.default_selection() is MediaSelection.IMAGES


def test_selection_filters_reel_media(reel_post: FakePost) -> None:
    model = build_post_model(reel_post, url=REEL_URL, url_hint="reel")

    assert [f.filename for f in model.items_for_selection(MediaSelection.VIDEO)] == [
        "video.mp4"
    ]
    assert [f.filename for f in model.items_for_selection(MediaSelection.COVER)] == [
        "cover.jpg"
    ]
    assert [
        f.filename for f in model.items_for_selection(MediaSelection.VIDEO_AND_COVER)
    ] == ["video.mp4", "cover.jpg"]


def test_selection_filters_carousel_media(mixed_carousel_post: FakePost) -> None:
    model = build_post_model(mixed_carousel_post, url=POST_URL, url_hint="p")

    assert [f.filename for f in model.items_for_selection(MediaSelection.IMAGES)] == [
        "001.jpg",
        "003.jpg",
    ]
    assert [f.filename for f in model.items_for_selection(MediaSelection.VIDEOS)] == [
        "002.mp4",
        "004.mp4",
    ]
    assert [
        f.filename
        for f in model.items_for_selection(MediaSelection.IMAGES_AND_VIDEOS)
    ] == ["001.jpg", "002.mp4", "003.jpg", "004.mp4"]


# ------------------------------------------------------------------ resolve_post


def test_resolve_post_end_to_end(reel_post: FakePost) -> None:
    client = FakeClient(post=reel_post)
    model = resolve_post(REEL_URL, client)

    assert client.requested == ["ABC123"], "exactly one fetch during analyze"
    assert model.content_type is ContentType.REEL
    assert model.shortcode == "ABC123"


def test_resolve_post_validates_url_before_fetching() -> None:
    client = FakeClient(post=None)
    with pytest.raises(ValidationError):
        resolve_post("https://example.com/not-instagram", client)
    assert client.requested == [], "no network call for an invalid URL"


def test_resolve_post_propagates_client_errors() -> None:
    client = FakeClient(error=InstagramError("boom"))
    with pytest.raises(InstagramError):
        resolve_post(POST_URL, client)


def test_resolve_post_accepts_username_prefixed_reel_url(reel_post: FakePost) -> None:
    client = FakeClient(post=reel_post)
    model = resolve_post("https://www.instagram.com/creator/reel/ABC123/", client)
    assert model.shortcode == "ABC123"
