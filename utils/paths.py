"""Local and remote path construction.

The Instagram shortcode is the primary content identifier. Timestamps are used
only to group content into year/month folders, never as the sole identifier.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from models.media import MediaType

#: Characters that are unsafe in a path segment on any common filesystem.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Reserved device names on Windows, guarded against for portability.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_segment(segment: str, fallback: str = "unknown") -> str:
    """Make a single path segment safe to use on disk."""
    cleaned = _UNSAFE.sub("_", (segment or "").strip())
    cleaned = cleaned.strip(". ")
    if cleaned.upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:120] or fallback


def post_relative_path(username: str, created_at: datetime, shortcode: str) -> Path:
    """Return ``username/YYYY/MM/SHORTCODE`` as a relative path."""
    return Path(
        sanitize_segment(username, "unknown_user"),
        f"{created_at.year:04d}",
        f"{created_at.month:02d}",
        sanitize_segment(shortcode, "unknown_post"),
    )


def post_directory(
    download_root: Path | str,
    username: str,
    created_at: datetime,
    shortcode: str,
) -> Path:
    """Absolute local staging directory for a post."""
    return Path(download_root) / post_relative_path(username, created_at, shortcode)


def remote_path_segments(
    root_folder: str,
    username: str,
    created_at: datetime,
    shortcode: str,
    *,
    use_username: bool = True,
    use_year: bool = True,
    use_month: bool = True,
    use_shortcode: bool = True,
    use_date_folder: bool = False,
) -> list[str]:
    """Build the Google Drive folder chain, each level individually toggleable.

    Google Drive has no path-based addressing, so the uploader walks this list
    and resolves or creates one folder per level, nesting by parent ID.

    ``use_date_folder`` replaces the separate year and month levels with one
    ``YYYY_MM_DD`` folder for the post's publish date, e.g.
    ``Root/2026_09_21/ABC123XYZ``.
    """
    segments = [sanitize_segment(root_folder, "Instagram-Knowledge-Base")]
    if use_username:
        segments.append(sanitize_segment(username, "unknown_user"))
    if use_date_folder:
        segments.append(date_folder_name(created_at))
    else:
        if use_year:
            segments.append(f"{created_at.year:04d}")
        if use_month:
            segments.append(f"{created_at.month:02d}")
    if use_shortcode:
        segments.append(sanitize_segment(shortcode, "unknown_post"))
    return segments


def date_folder_name(created_at: datetime) -> str:
    """Single date folder name, ``YYYY_MM_DD``, for the post's publish date."""
    return (
        f"{created_at.year:04d}_{created_at.month:02d}_{created_at.day:02d}"
    )


def remote_path_display(segments: list[str]) -> str:
    """Render folder segments as a slash-joined string for the UI."""
    return "/".join(segments)


def carousel_filename(index: int, media_type: MediaType) -> str:
    """Zero-padded, order-preserving filename for a carousel child.

    Produces ``001.jpg``, ``002.mp4`` and so on so that the original ordering
    survives on disk.
    """
    extension = "mp4" if media_type is MediaType.VIDEO else "jpg"
    return f"{index:03d}.{extension}"


def ensure_directory(path: Path | str) -> Path:
    """Create a directory (and parents) if missing, returning it."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def unique_path(path: Path) -> Path:
    """Return a non-colliding variant of ``path`` by appending ``_1``, ``_2``...

    Used when a user deliberately re-downloads content that already exists and
    we must not clobber the previous copy.
    """
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def directory_size(path: Path | str) -> int:
    """Total size in bytes of all files under ``path``."""
    root = Path(path)
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
