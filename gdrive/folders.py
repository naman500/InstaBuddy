"""Google Drive folder resolution.

Drive has no path-based addressing. Unlike a filesystem, you cannot ask for
``/a/b/c`` directly: each level must be looked up or created by name within its
parent, and referenced thereafter by ID.

This module walks a list of folder names and returns the leaf folder ID,
creating any missing levels. Resolved IDs are cached per instance so a batch of
uploads into the same folder costs one lookup chain, not one per file.
"""

from __future__ import annotations

import logging
from datetime import datetime

from gdrive.client import DriveClient
from utils.paths import remote_path_display, remote_path_segments

logger = logging.getLogger(__name__)


class FolderResolver:
    """Resolves folder name chains to Drive folder IDs, creating as needed."""

    def __init__(self, client: DriveClient) -> None:
        self.client = client
        #: Maps ``parent_id/name`` to a folder ID.
        self._cache: dict[str, str] = {}

    def resolve_chain(self, segments: list[str], *, create: bool = True) -> str | None:
        """Resolve a chain of folder names to the leaf folder ID.

        Args:
            segments: Folder names from the root downwards.
            create: When False, returns None if any level is missing instead of
                creating it. Used by the destination preview.
        """
        parent_id: str | None = None

        for name in segments:
            if not name:
                continue
            cache_key = f"{parent_id or 'root'}/{name}"

            cached = self._cache.get(cache_key)
            if cached:
                parent_id = cached
                continue

            existing = self.client.find_child(name, parent_id, folders_only=True)
            if existing:
                parent_id = existing["id"]
            elif create:
                parent_id = self.client.create_folder(name, parent_id)["id"]
            else:
                return None

            self._cache[cache_key] = parent_id

        return parent_id

    def resolve_for_post(
        self,
        *,
        root_folder: str,
        username: str,
        created_at: datetime,
        shortcode: str,
        use_username: bool = True,
        use_year: bool = True,
        use_month: bool = True,
        use_shortcode: bool = True,
        use_date_folder: bool = False,
        create: bool = True,
    ) -> tuple[str | None, str]:
        """Resolve the destination folder for a post.

        Returns ``(folder_id, display_path)``. The display path is always
        returned so the UI can show the destination even when the folder does
        not exist yet.
        """
        segments = remote_path_segments(
            root_folder,
            username,
            created_at,
            shortcode,
            use_username=use_username,
            use_year=use_year,
            use_month=use_month,
            use_shortcode=use_shortcode,
            use_date_folder=use_date_folder,
        )
        display = remote_path_display(segments)
        folder_id = self.resolve_chain(segments, create=create)

        if folder_id and create:
            logger.info("Drive folder ready path=%s id=%s", display, folder_id)
        return folder_id, display

    def clear_cache(self) -> None:
        self._cache.clear()
