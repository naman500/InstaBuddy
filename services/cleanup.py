"""Post-upload cleanup.

The governing rule: never delete a local file before its upload has been
verified. A user must not lose downloaded media because of a Drive error.

Cleanup therefore only ever runs when every file in a post uploaded
successfully, and only when the destination mode says no local copy is wanted.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from models.media import DestinationMode
from models.upload import UploadResult, UploadStatus

logger = logging.getLogger(__name__)


def should_cleanup(
    destination_mode: DestinationMode,
    upload_results: list[UploadResult],
    *,
    cleanup_enabled: bool = True,
) -> bool:
    """Decide whether staged files may be removed.

    All four conditions must hold: cleanup is enabled, the mode wants no local
    copy, at least one upload happened, and every upload succeeded.
    """
    if not cleanup_enabled:
        return False
    if destination_mode.keeps_local_copy:
        return False
    if not upload_results:
        return False
    return all(result.status is UploadStatus.UPLOADED for result in upload_results)


def cleanup_directory(directory: Path | str, *, dry_run: bool = False) -> bool:
    """Remove a staging directory. Returns True if it was removed.

    Failures are logged and swallowed: a cleanup problem must never turn an
    otherwise successful ingestion into a failure.
    """
    target = Path(directory)
    if not target.is_dir():
        return False

    if dry_run:
        logger.info("Cleanup (dry run) would remove %s", target)
        return False

    try:
        shutil.rmtree(target)
    except OSError as exc:
        logger.warning("Could not clean up %s: %s", target, exc)
        return False

    logger.info("Cleaned up staging directory %s", target)
    _prune_empty_parents(target.parent)
    return True


def _prune_empty_parents(directory: Path, depth: int = 3) -> None:
    """Remove now-empty year/month/username folders left behind.

    Bounded to three levels so it can never walk up past the download root.
    """
    current = directory
    for _ in range(depth):
        try:
            if not current.is_dir() or any(current.iterdir()):
                return
            current.rmdir()
        except OSError:
            return
        current = current.parent


def remove_partial_files(directory: Path | str) -> int:
    """Delete leftover ``.part`` files from an interrupted download."""
    target = Path(directory)
    if not target.is_dir():
        return 0

    removed = 0
    for path in target.glob("*.part"):
        try:
            path.unlink()
            removed += 1
        except OSError:  # pragma: no cover - best effort
            logger.debug("Could not remove %s", path)
    return removed
