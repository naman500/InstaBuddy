"""Media taxonomy and user-facing selection options.

These enums are deliberately free of any Instagram or Google Drive specifics so
they can be reused by the storage, metadata and UI layers.
"""

from __future__ import annotations

from enum import Enum


class MediaType(str, Enum):
    """The type of a single concrete file on disk.

    ``DOCUMENT`` covers the generated ``metadata.json`` and ``document.txt``.
    They are tracked in the manifest alongside media so their hashes and upload
    status are visible, but they are never offered as a download choice.
    """

    IMAGE = "image"
    VIDEO = "video"
    DOCUMENT = "document"


class ContentType(str, Enum):
    """The type of an Instagram post as a whole."""

    IMAGE = "image"
    VIDEO = "video"
    CAROUSEL = "carousel"
    REEL = "reel"


class MediaSelection(str, Enum):
    """What the user asked to download.

    The available options depend on the resolved content type; see
    :meth:`models.instagram.InstagramPost.available_selections`.
    """

    # Single image post
    IMAGE = "image"

    # Reel / video post
    VIDEO = "video"
    COVER = "cover"
    VIDEO_AND_COVER = "video+cover"

    # Carousel
    IMAGES = "images"
    VIDEOS = "videos"
    IMAGES_AND_VIDEOS = "images+videos"

    @property
    def label(self) -> str:
        """Human-readable label for the UI."""
        return {
            MediaSelection.IMAGE: "Image",
            MediaSelection.VIDEO: "Video",
            MediaSelection.COVER: "Cover image",
            MediaSelection.VIDEO_AND_COVER: "Video + cover image",
            MediaSelection.IMAGES: "Images only",
            MediaSelection.VIDEOS: "Videos only",
            MediaSelection.IMAGES_AND_VIDEOS: "Images + videos",
        }[self]

    @property
    def includes_video(self) -> bool:
        return self in {
            MediaSelection.VIDEO,
            MediaSelection.VIDEO_AND_COVER,
            MediaSelection.VIDEOS,
            MediaSelection.IMAGES_AND_VIDEOS,
        }

    @property
    def includes_image(self) -> bool:
        return self in {
            MediaSelection.IMAGE,
            MediaSelection.COVER,
            MediaSelection.VIDEO_AND_COVER,
            MediaSelection.IMAGES,
            MediaSelection.IMAGES_AND_VIDEOS,
        }


class DestinationMode(str, Enum):
    """Where the downloaded content should end up.

    ``DRIVE_ONLY`` still stages files locally first, because media must be
    retrieved before it can be uploaded. Staged files are only removed after the
    upload has been verified.
    """

    LOCAL_ONLY = "LOCAL_ONLY"
    DRIVE_ONLY = "DRIVE_ONLY"
    LOCAL_AND_DRIVE = "LOCAL_AND_DRIVE"

    @property
    def label(self) -> str:
        return {
            DestinationMode.LOCAL_ONLY: "Local only",
            DestinationMode.DRIVE_ONLY: "Google Drive",
            DestinationMode.LOCAL_AND_DRIVE: "Local + Google Drive",
        }[self]

    @property
    def uploads_to_drive(self) -> bool:
        return self in {DestinationMode.DRIVE_ONLY, DestinationMode.LOCAL_AND_DRIVE}

    @property
    def keeps_local_copy(self) -> bool:
        return self in {DestinationMode.LOCAL_ONLY, DestinationMode.LOCAL_AND_DRIVE}
