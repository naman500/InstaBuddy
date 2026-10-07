"""Metadata generation.

Two artifacts accompany every ingested post:

* ``metadata.json`` - structured, schema-versioned, machine-readable
* ``document.txt`` - a flat text rendering intended as the future RAG
  pipeline's text surface for the post

The metadata is the actual product here. The media files are almost a side
effect, so this module is deliberately explicit about what gets recorded.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from models.instagram import InstagramPost
from models.metadata import (
    APPLICATION_VERSION,
    MetadataContent,
    MetadataCreator,
    MetadataIngestion,
    MetadataMediaEntry,
    MetadataProcessing,
    MetadataSource,
    PostMetadata,
)
from models.upload import DownloadedFile

logger = logging.getLogger(__name__)

METADATA_FILENAME = "metadata.json"
DOCUMENT_FILENAME = "document.txt"

#: Human-readable content type labels for document.txt.
_CONTENT_LABELS = {
    "reel": "Reel",
    "video": "Video",
    "image": "Image",
    "carousel": "Carousel",
}


def build_metadata(
    post: InstagramPost,
    files: list[DownloadedFile],
    *,
    downloaded_at: datetime | None = None,
    job_id: str | None = None,
    downloaded_by: str | None = None,
) -> PostMetadata:
    """Assemble the ``metadata.json`` model for a post."""
    return PostMetadata(
        source=MetadataSource(
            platform="instagram",
            url=post.url,
            shortcode=post.shortcode,
        ),
        creator=MetadataCreator(username=post.username),
        content=MetadataContent(
            type=post.content_type.value,
            caption=post.caption,
            created_at=post.created_at,
        ),
        media=[
            MetadataMediaEntry(
                filename=f.filename,
                type=f.media_type.value,
                sha256=f.sha256,
                file_size=f.file_size,
            )
            for f in files
        ],
        ingestion=MetadataIngestion(
            downloaded_at=downloaded_at or datetime.now(timezone.utc),
            application_version=APPLICATION_VERSION,
            job_id=job_id,
            downloaded_by=downloaded_by,
        ),
        processing=MetadataProcessing(),
    )


def build_document(post: InstagramPost, files: list[DownloadedFile]) -> str:
    """Render the flat ``document.txt`` text for a post.

    Kept plain and predictable so a future chunker can parse it without
    guessing. Sections appear in a fixed order even when empty.
    """
    label = _CONTENT_LABELS.get(post.content_type.value, post.content_type.value.title())
    created = post.created_at.isoformat() if post.created_at else ""
    caption = (post.caption or "").strip() or "(no caption)"

    lines: list[str] = [
        "SOURCE: Instagram",
        "",
        f"CONTENT TYPE: {label}",
        "",
        f"AUTHOR: {post.username}",
        "",
        "ORIGINAL URL:",
        post.url,
        "",
        "CREATED AT:",
        created,
        "",
        "CAPTION:",
        "",
        caption,
        "",
        "MEDIA:",
        "",
    ]
    lines.extend(f.filename for f in files)
    return "\n".join(lines) + "\n"


def write_metadata(
    directory: Path | str,
    post: InstagramPost,
    files: list[DownloadedFile],
    *,
    downloaded_at: datetime | None = None,
    job_id: str | None = None,
    downloaded_by: str | None = None,
) -> tuple[Path, Path]:
    """Write both artifacts and return ``(metadata_path, document_path)``."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    metadata = build_metadata(
        post,
        files,
        downloaded_at=downloaded_at,
        job_id=job_id,
        downloaded_by=downloaded_by,
    )
    metadata_path = target / METADATA_FILENAME
    metadata_path.write_text(metadata.to_json() + "\n", encoding="utf-8")

    document_path = target / DOCUMENT_FILENAME
    document_path.write_text(build_document(post, files), encoding="utf-8")

    logger.info(
        "Metadata written shortcode=%s files=%d directory=%s",
        post.shortcode,
        len(files),
        target,
    )
    return metadata_path, document_path


def read_metadata(path: Path | str) -> dict:
    """Load a ``metadata.json`` from disk.

    Returns an empty dict when the file is missing or unreadable, so the
    History page can degrade gracefully rather than failing to render.
    """
    metadata_path = Path(path)
    if not metadata_path.is_file():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read metadata at %s: %s", metadata_path, exc)
        return {}
