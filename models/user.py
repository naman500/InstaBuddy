"""Dashboard user model.

Note the deliberate absence of any password or hash field: a user object can
therefore never accidentally serialize a credential into a log, a session or an
API response.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    """Two roles are sufficient for V1.

    The field exists so finer-grained permissions can be added later without a
    schema change.
    """

    ADMIN = "admin"
    VIEWER = "viewer"

    @property
    def label(self) -> str:
        return {Role.ADMIN: "Administrator", Role.VIEWER: "Read only"}[self]


class AppUser(BaseModel):
    """An authenticated dashboard user."""

    username: str
    display_name: str
    role: Role = Role.VIEWER

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def can_download(self) -> bool:
        """Viewers may browse history but not start ingestion runs."""
        return self.role is Role.ADMIN
