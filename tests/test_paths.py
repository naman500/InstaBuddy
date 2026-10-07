"""Local folder generation and Google Drive path construction."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from models.media import MediaType
from utils.paths import (
    carousel_filename,
    directory_size,
    post_directory,
    post_relative_path,
    remote_path_display,
    remote_path_segments,
    sanitize_segment,
    unique_path,
)

CREATED = datetime(2026, 9, 21, 10, 0, 0)


def test_post_relative_path_uses_shortcode_as_leaf() -> None:
    path = post_relative_path("techcreator", CREATED, "Cx123ABC")
    assert path == Path("techcreator/2026/09/Cx123ABC")


def test_month_is_zero_padded() -> None:
    path = post_relative_path("user", datetime(2026, 1, 5), "ABC")
    assert path == Path("user/2026/01/ABC")


def test_post_directory_is_absolute_under_root() -> None:
    directory = post_directory("/tmp/downloads", "user", CREATED, "ABC123")
    assert directory == Path("/tmp/downloads/user/2026/09/ABC123")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("normal_user", "normal_user"),
        ("with/slash", "with_slash"),
        ("with\\backslash", "with_backslash"),
        ("with:colon", "with_colon"),
        # Separators become underscores, then leading dots are stripped so the
        # result can be neither a traversal nor a hidden file.
        ("../../etc/passwd", "_.._etc_passwd"),
        (".hidden", "hidden"),
        ("..", "unknown"),
        ("trailing.", "trailing"),
        ("  spaced  ", "spaced"),
        ("", "unknown"),
    ],
)
def test_sanitize_segment(raw: str, expected: str) -> None:
    assert sanitize_segment(raw) == expected


@pytest.mark.parametrize(
    "hostile",
    ["../../../etc", "..", "../", "/absolute/path", "a/../../b", "\\\\server\\share"],
)
def test_sanitize_blocks_path_traversal(hostile: str) -> None:
    """A hostile username must not escape the download root."""
    segment = sanitize_segment(hostile)
    assert "/" not in segment
    assert "\\" not in segment
    assert segment != ".."
    # Joining it must stay inside the root.
    root = Path("/downloads")
    assert (root / segment).resolve().is_relative_to(root)


def test_sanitize_guards_windows_reserved_names() -> None:
    assert sanitize_segment("CON") == "_CON"
    assert sanitize_segment("com1") == "_com1"


def test_sanitize_truncates_long_segments() -> None:
    assert len(sanitize_segment("x" * 500)) == 120


def test_carousel_filenames_preserve_order() -> None:
    names = [
        carousel_filename(1, MediaType.IMAGE),
        carousel_filename(2, MediaType.VIDEO),
        carousel_filename(3, MediaType.IMAGE),
    ]
    assert names == ["001.jpg", "002.mp4", "003.jpg"]
    assert names == sorted(names), "lexical sort must match original ordering"


def test_carousel_filename_pads_to_three_digits() -> None:
    assert carousel_filename(10, MediaType.IMAGE) == "010.jpg"
    assert carousel_filename(100, MediaType.VIDEO) == "100.mp4"


def test_remote_segments_full_tree() -> None:
    segments = remote_path_segments(
        "Instagram-Knowledge-Base", "techcreator", CREATED, "Cx123ABC"
    )
    assert segments == ["Instagram-Knowledge-Base", "techcreator", "2026", "09", "Cx123ABC"]
    assert (
        remote_path_display(segments)
        == "Instagram-Knowledge-Base/techcreator/2026/09/Cx123ABC"
    )


def test_remote_segments_respect_toggles() -> None:
    segments = remote_path_segments(
        "Root",
        "user",
        CREATED,
        "ABC",
        use_username=False,
        use_year=True,
        use_month=False,
        use_shortcode=True,
    )
    assert segments == ["Root", "2026", "ABC"]


def test_remote_segments_root_only() -> None:
    segments = remote_path_segments(
        "Root",
        "user",
        CREATED,
        "ABC",
        use_username=False,
        use_year=False,
        use_month=False,
        use_shortcode=False,
    )
    assert segments == ["Root"]


def test_unique_path_avoids_clobbering(tmp_path: Path) -> None:
    target = tmp_path / "video.mp4"
    assert unique_path(target) == target

    target.write_bytes(b"first")
    assert unique_path(target) == tmp_path / "video_1.mp4"

    (tmp_path / "video_1.mp4").write_bytes(b"second")
    assert unique_path(target) == tmp_path / "video_2.mp4"


def test_directory_size(tmp_path: Path) -> None:
    assert directory_size(tmp_path / "missing") == 0
    (tmp_path / "a.txt").write_bytes(b"12345")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b.txt").write_bytes(b"123")
    assert directory_size(tmp_path) == 8


def test_date_folder_layout() -> None:
    from utils.paths import date_folder_name, remote_path_segments

    created = datetime(2026, 9, 1)
    assert date_folder_name(created) == "2026_09_01"
    segments = remote_path_segments(
        "Root",
        "creator",
        created,
        "ABC123XYZ",
        use_username=False,
        use_date_folder=True,
    )
    assert segments == ["Root", "2026_09_01", "ABC123XYZ"]


def test_date_folder_replaces_year_and_month() -> None:
    from utils.paths import remote_path_segments

    segments = remote_path_segments(
        "Root", "creator", datetime(2026, 9, 21), "ABC", use_date_folder=True
    )
    assert segments == ["Root", "creator", "2026_09_21", "ABC"]
