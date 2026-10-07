"""Pydantic models describing resolved Instagram content."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from models.media import ContentType, MediaSelection, MediaType


class InstagramMediaItem(BaseModel):
    """A single downloadable media item belonging to a post.

    ``index`` preserves the original ordering of carousel children so that
    files land on disk as ``001.jpg``, ``002.mp4`` and so on.
    """

    index: int = Field(ge=1, description="1-based position within the post")
    media_type: MediaType
    source_url: str
    filename: str
    is_video: bool = False
    is_cover: bool = False

    @property
    def is_image(self) -> bool:
        return self.media_type is MediaType.IMAGE


class InstagramPost(BaseModel):
    """The result of resolving an Instagram URL.

    This is produced by :mod:`instagram.resolver` during the *analyze* phase and
    contains everything the UI needs to offer download options, without having
    written a single byte to disk.
    """

    id: str
    shortcode: str
    url: str
    username: str
    caption: str | None = None
    created_at: datetime
    content_type: ContentType
    is_video: bool = False
    is_carousel: bool = False
    media_items: list[InstagramMediaItem] = Field(default_factory=list)

    @property
    def media_count(self) -> int:
        """Number of media items, excluding a Reel/video cover image."""
        return len([item for item in self.media_items if not item.is_cover])

    @property
    def video_count(self) -> int:
        return len([i for i in self.media_items if i.is_video])

    @property
    def image_count(self) -> int:
        return len([i for i in self.media_items if i.is_image and not i.is_cover])

    @property
    def has_cover(self) -> bool:
        return any(item.is_cover for item in self.media_items)

    def available_selections(self) -> list[MediaSelection]:
        """Download options valid for this post's content type.

        A plain image post is never offered a video option, and a carousel is
        only offered the sub-types it actually contains.
        """
        if self.content_type is ContentType.IMAGE:
            return [MediaSelection.IMAGE]

        if self.content_type in (ContentType.REEL, ContentType.VIDEO):
            options = [MediaSelection.VIDEO]
            if self.has_cover:
                options += [MediaSelection.COVER, MediaSelection.VIDEO_AND_COVER]
            return options

        # Carousel: only offer what is present.
        options = []
        if self.image_count:
            options.append(MediaSelection.IMAGES)
        if self.video_count:
            options.append(MediaSelection.VIDEOS)
        if self.image_count and self.video_count:
            options.append(MediaSelection.IMAGES_AND_VIDEOS)
        return options

    def default_selection(self) -> MediaSelection:
        """The pre-selected option, per the product spec."""
        available = self.available_selections()
        if self.content_type is ContentType.IMAGE:
            return MediaSelection.IMAGE
        if self.content_type in (ContentType.REEL, ContentType.VIDEO):
            return MediaSelection.VIDEO
        # Carousel default is everything it contains.
        if MediaSelection.IMAGES_AND_VIDEOS in available:
            return MediaSelection.IMAGES_AND_VIDEOS
        return available[0]

    def items_for_selection(
        self, selection: MediaSelection
    ) -> list[InstagramMediaItem]:
        """Filter media items down to those the selection asks for.

        Ordering is always preserved.
        """
        if selection is MediaSelection.IMAGE:
            return [i for i in self.media_items if i.is_image]

        if selection is MediaSelection.VIDEO:
            return [i for i in self.media_items if i.is_video]

        if selection is MediaSelection.COVER:
            return [i for i in self.media_items if i.is_cover]

        if selection is MediaSelection.VIDEO_AND_COVER:
            return [i for i in self.media_items if i.is_video or i.is_cover]

        if selection is MediaSelection.IMAGES:
            return [i for i in self.media_items if i.is_image and not i.is_cover]

        if selection is MediaSelection.VIDEOS:
            return [i for i in self.media_items if i.is_video]

        if selection is MediaSelection.IMAGES_AND_VIDEOS:
            return [i for i in self.media_items if not i.is_cover]

        return []
