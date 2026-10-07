"""Cleanup policy.

The rule under test throughout: local files are never removed unless every
upload succeeded and the user asked for no local copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from models.media import DestinationMode
from models.upload import UploadResult, UploadStatus
from services.cleanup import (
    cleanup_directory,
    remove_partial_files,
    should_cleanup,
)


def upload(status: UploadStatus, filename: str = "video.mp4") -> UploadResult:
    return UploadResult(
        account="personal",
        filename=filename,
        remote_path=f"Root/{filename}",
        status=status,
    )


ALL_GOOD = [upload(UploadStatus.UPLOADED), upload(UploadStatus.UPLOADED, "cover.jpg")]
ONE_BAD = [upload(UploadStatus.UPLOADED), upload(UploadStatus.FAILED, "cover.jpg")]


# ------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "mode,results,enabled,expected",
    [
        # Only DRIVE_ONLY with everything uploaded and cleanup enabled.
        (DestinationMode.DRIVE_ONLY, ALL_GOOD, True, True),
        # Cleanup switched off.
        (DestinationMode.DRIVE_ONLY, ALL_GOOD, False, False),
        # Modes that keep a local copy never clean up.
        (DestinationMode.LOCAL_ONLY, ALL_GOOD, True, False),
        (DestinationMode.LOCAL_AND_DRIVE, ALL_GOOD, True, False),
        # A single failure blocks cleanup.
        (DestinationMode.DRIVE_ONLY, ONE_BAD, True, False),
        # Nothing uploaded at all.
        (DestinationMode.DRIVE_ONLY, [], True, False),
        (
            DestinationMode.DRIVE_ONLY,
            [upload(UploadStatus.PENDING)],
            True,
            False,
        ),
    ],
)
def test_should_cleanup_matrix(mode, results, enabled, expected) -> None:
    assert should_cleanup(mode, results, cleanup_enabled=enabled) is expected


def test_pending_upload_blocks_cleanup() -> None:
    """An unfinished upload is not a successful one."""
    results = [upload(UploadStatus.UPLOADED), upload(UploadStatus.UPLOADING, "c.jpg")]
    assert should_cleanup(DestinationMode.DRIVE_ONLY, results, cleanup_enabled=True) is False


# ---------------------------------------------------------------- filesystem


def test_cleanup_removes_directory(tmp_path: Path) -> None:
    directory = tmp_path / "creator" / "2026" / "09" / "ABC123"
    directory.mkdir(parents=True)
    (directory / "video.mp4").write_bytes(b"data")

    assert cleanup_directory(directory) is True
    assert not directory.exists()


def test_cleanup_prunes_empty_parents(tmp_path: Path) -> None:
    directory = tmp_path / "creator" / "2026" / "09" / "ABC123"
    directory.mkdir(parents=True)
    (directory / "video.mp4").write_bytes(b"data")

    cleanup_directory(directory)

    assert not (tmp_path / "creator" / "2026" / "09").exists()
    assert not (tmp_path / "creator").exists()


def test_cleanup_keeps_parents_that_still_have_content(tmp_path: Path) -> None:
    month = tmp_path / "creator" / "2026" / "09"
    first = month / "ABC123"
    second = month / "XYZ789"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (second / "keep.jpg").write_bytes(b"keep")

    cleanup_directory(first)

    assert not first.exists()
    assert second.exists(), "a sibling post must not be pruned"
    assert month.exists()


def test_cleanup_missing_directory_is_noop(tmp_path: Path) -> None:
    assert cleanup_directory(tmp_path / "nope") is False


def test_cleanup_dry_run_removes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "post"
    directory.mkdir()
    (directory / "video.mp4").write_bytes(b"data")

    assert cleanup_directory(directory, dry_run=True) is False
    assert (directory / "video.mp4").is_file()


def test_remove_partial_files(tmp_path: Path) -> None:
    (tmp_path / "video.mp4").write_bytes(b"done")
    (tmp_path / "video.mp4.part").write_bytes(b"partial")
    (tmp_path / "cover.jpg.part").write_bytes(b"partial")

    assert remove_partial_files(tmp_path) == 2
    assert (tmp_path / "video.mp4").is_file(), "finished files are untouched"
    assert list(tmp_path.glob("*.part")) == []


def test_remove_partial_files_missing_directory(tmp_path: Path) -> None:
    assert remove_partial_files(tmp_path / "nope") == 0
