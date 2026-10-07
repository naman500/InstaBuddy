"""Media downloader.

Fetches selected media items to a local staging directory, then hashes each
file. Downloads stream to a ``.part`` file and are renamed into place only once
complete, so an interrupted run can never leave a truncated file that looks
valid.

Media transfers go through Instaloader's own documented ``get_raw`` so that all
Instagram HTTP traffic shares one code path and one rate-limiting policy.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from instaloader import exceptions as ig_errors

from models.instagram import InstagramMediaItem, InstagramPost
from models.upload import DownloadedFile
from services.hashing import calculate_sha256
from instagram.client import is_rate_limited, rate_limit_error
from utils.errors import (
    InstagramContentUnavailableError,
    InstagramDownloadError,
)
from utils.paths import ensure_directory, sanitize_segment

logger = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 1.0
_STREAM_CHUNK = 1024 * 256
PART_SUFFIX = ".part"


@dataclass
class DownloadProgress:
    """Snapshot of download progress, for the UI."""

    current_index: int
    total_items: int
    filename: str
    bytes_done: int = 0
    bytes_total: int | None = None

    @property
    def fraction(self) -> float:
        """Overall completion across all items, in the range 0.0 to 1.0."""
        if self.total_items <= 0:
            return 0.0
        completed = self.current_index - 1
        within = 0.0
        if self.bytes_total:
            within = min(self.bytes_done / self.bytes_total, 1.0)
        return min((completed + within) / self.total_items, 1.0)

    @property
    def message(self) -> str:
        return f"Downloading media {self.current_index}/{self.total_items}..."


ProgressCallback = Callable[[DownloadProgress], None]


class _RawFetcher(Protocol):
    def __call__(self, url: str) -> object: ...


class MediaDownloader:
    """Downloads resolved media items to disk."""

    def __init__(
        self,
        client: object,
        *,
        max_retries: int = 3,
        fetch: _RawFetcher | None = None,
    ) -> None:
        """
        Args:
            client: An :class:`instagram.client.InstagramClient`.
            max_retries: Total attempts per file, including the first.
            fetch: Override for the raw fetch function. Tests inject a fake;
                production uses Instaloader's ``context.get_raw``.
        """
        self._client = client
        self.max_retries = max(1, min(int(max_retries), 5))
        if fetch is not None:
            self._fetch = fetch
        else:
            context = getattr(client, "context", None)
            if context is None or not hasattr(context, "get_raw"):
                raise InstagramDownloadError(
                    "Instagram client cannot fetch media",
                    user_message="The Instagram connection is not ready.",
                )
            self._fetch = context.get_raw

    # ----------------------------------------------------------------- public

    def download_items(
        self,
        post: InstagramPost,
        items: list[InstagramMediaItem],
        directory: Path | str,
        *,
        progress: ProgressCallback | None = None,
    ) -> list[DownloadedFile]:
        """Download ``items`` into ``directory`` and hash each result.

        Returns files in the same order as ``items``.

        Raises:
            InstagramDownloadError: if any item cannot be downloaded.
        """
        if not items:
            raise InstagramDownloadError(
                "No media items selected",
                user_message="Please choose at least one item to download.",
            )

        target_dir = ensure_directory(directory)
        total = len(items)
        results: list[DownloadedFile] = []

        logger.info(
            "Download started shortcode=%s items=%d directory=%s",
            post.shortcode,
            total,
            target_dir,
        )

        for position, item in enumerate(items, start=1):
            filename = sanitize_segment(item.filename, f"{position:03d}.bin")
            destination = target_dir / filename

            if progress is not None:
                progress(DownloadProgress(position, total, filename))

            size = self._download_one(
                item,
                destination,
                on_bytes=(
                    None
                    if progress is None
                    else lambda done, total_bytes, p=position, f=filename: progress(
                        DownloadProgress(p, total, f, done, total_bytes)
                    )
                ),
            )

            sha256 = calculate_sha256(destination)
            results.append(
                DownloadedFile(
                    filename=filename,
                    path=destination,
                    media_type=item.media_type,
                    file_size=size,
                    sha256=sha256,
                )
            )
            logger.info("File downloaded name=%s bytes=%d", filename, size)

        return results

    # ---------------------------------------------------------------- private

    def _download_one(
        self,
        item: InstagramMediaItem,
        destination: Path,
        *,
        on_bytes: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """Download a single item, returning its size in bytes."""
        part_path = destination.with_name(destination.name + PART_SUFFIX)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                size = self._stream_to_file(
                    item.source_url, part_path, on_bytes=on_bytes
                )

                if size <= 0:
                    raise InstagramDownloadError(
                        f"Downloaded zero bytes for {item.filename}",
                        user_message=(
                            f"The file '{item.filename}' came back empty. "
                            "Please try again."
                        ),
                    )

                part_path.replace(destination)
                return size

            except ig_errors.QueryReturnedForbiddenException as exc:
                # Signed CDN URLs expire. That is not something to retry around.
                self._cleanup_part(part_path)
                raise InstagramContentUnavailableError(
                    f"Media URL rejected for {item.filename}: {exc}",
                    user_message=(
                        "Instagram refused to serve this media. The link may have "
                        "expired, so please analyze the post again."
                    ),
                ) from exc

            except (ig_errors.ConnectionException, OSError) as exc:
                last_error = exc
                self._cleanup_part(part_path)
                # A 429 may arrive as TooManyRequestsException or as a plain
                # ConnectionException. Either way, stop: retrying within
                # seconds only extends the limit.
                if is_rate_limited(exc):
                    raise rate_limit_error(
                        f"downloading {item.filename}", exc
                    ) from exc
                if attempt >= self.max_retries:
                    raise InstagramDownloadError(
                        f"Could not download {item.filename}: {exc}",
                        user_message=(
                            f"'{item.filename}' could not be downloaded after "
                            f"{self.max_retries} attempts."
                        ),
                    ) from exc
                self._backoff(attempt, "download error")

            except InstagramDownloadError:
                self._cleanup_part(part_path)
                raise

        raise InstagramDownloadError(
            f"Could not download {item.filename}: {last_error}"
        )

    def _stream_to_file(
        self,
        url: str,
        part_path: Path,
        *,
        on_bytes: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """Stream ``url`` into ``part_path``, returning bytes written."""
        response = self._fetch(url)
        total_bytes = _content_length(response)
        written = 0

        try:
            with part_path.open("wb") as handle:
                iterator = getattr(response, "iter_content", None)
                if callable(iterator):
                    for chunk in iterator(chunk_size=_STREAM_CHUNK):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        if on_bytes is not None:
                            on_bytes(written, total_bytes)
                else:
                    # Fall back to the raw file-like object.
                    raw = getattr(response, "raw", None)
                    if raw is None:
                        raise InstagramDownloadError("Response exposes no content")
                    shutil.copyfileobj(raw, handle, _STREAM_CHUNK)
                    written = part_path.stat().st_size
                    if on_bytes is not None:
                        on_bytes(written, total_bytes)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        return written

    @staticmethod
    def _cleanup_part(part_path: Path) -> None:
        """Remove a partial file. Never touches the finished destination."""
        try:
            if part_path.exists():
                part_path.unlink()
        except OSError:  # pragma: no cover - best effort
            logger.debug("Could not remove partial file %s", part_path)

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        logger.warning(
            "Media %s; waiting %.0fs before attempt %d", reason, delay, attempt + 1
        )
        time.sleep(delay)


def _content_length(response: object) -> int | None:
    """Best-effort Content-Length, used only for progress display."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("Content-Length") or headers.get("content-length")
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None
