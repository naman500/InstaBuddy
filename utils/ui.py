"""Streamlit helpers: login gate, session state and shared widgets.

This is the only place outside ``pages/`` and ``app.py`` that imports Streamlit.
Business logic lives in :mod:`services` and knows nothing about the UI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st

from auth.app_auth import authenticate, has_configured_users
from models.user import AppUser, Role
from services.ingestion import IngestionService
from services.jobs import JobService
from services.manifest import Manifest
from utils.config import AppConfig, get_auth_settings, get_config
from utils.errors import AppAuthenticationError, AppError
from utils.logging_config import configure_logging

logger = logging.getLogger(__name__)

SESSION_USER = "auth_user"
SESSION_LAST_SEEN = "auth_last_seen"


# --------------------------------------------------------------- bootstrapping


def bootstrap() -> AppConfig:
    """Load config, create directories and configure logging.

    Safe to call on every rerun: logging guards against duplicate handlers and
    directory creation is idempotent.
    """
    config = get_config()
    config.ensure_directories()
    configure_logging(config.log_level, config.log_path)
    return config


@st.cache_resource(show_spinner=False)
def get_manifest(db_path: str) -> Manifest:
    """One shared manifest per database path.

    Cached as a resource so every rerun does not reopen and re-verify the schema.
    """
    return Manifest(db_path)


def get_job_service(config: AppConfig) -> JobService:
    return JobService(get_manifest(str(config.manifest_db_path)), config.download_path)


def get_ingestion_service(
    config: AppConfig, session_username: str | None = None
) -> IngestionService:
    """A fresh ingestion service.

    Not cached, because the Instagram session choice can change between runs.
    When ``session_username`` is given, the client loads that saved session;
    otherwise it operates in public mode with no login.
    """
    from instagram.client import InstagramClient

    manifest = get_manifest(str(config.manifest_db_path))

    def build_client() -> InstagramClient:
        return InstagramClient(
            session_username=session_username,
            session_dir=config.session_path if session_username else None,
            max_retries=config.max_instagram_retries,
        )

    return IngestionService(config, manifest, client_factory=build_client)


# ----------------------------------------------------------------- auth gate


def current_user() -> AppUser | None:
    """The signed-in user, or None."""
    raw = st.session_state.get(SESSION_USER)
    if raw is None:
        return None
    if isinstance(raw, AppUser):
        return raw
    try:
        return AppUser(**raw)
    except Exception:
        return None


def _session_expired(timeout_minutes: int) -> bool:
    last_seen = st.session_state.get(SESSION_LAST_SEEN)
    if not isinstance(last_seen, datetime):
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    return last_seen < cutoff


def _touch_session() -> None:
    st.session_state[SESSION_LAST_SEEN] = datetime.now(timezone.utc)


def sign_out(message: str | None = None) -> None:
    """Clear the session and return to the login form."""
    user = current_user()
    for key in (SESSION_USER, SESSION_LAST_SEEN):
        st.session_state.pop(key, None)
    if user is not None:
        logger.info("Sign out user=%s", user.username)
    if message:
        st.session_state["auth_notice"] = message
    st.rerun()


def require_login() -> AppUser:
    """Gate the current page.

    Renders the login form and halts the script when there is no valid session,
    so page content below the call can assume an authenticated user.
    """
    settings = get_auth_settings()
    user = current_user()

    if user is not None and _session_expired(settings["session_timeout_minutes"]):
        for key in (SESSION_USER, SESSION_LAST_SEEN):
            st.session_state.pop(key, None)
        st.session_state["auth_notice"] = (
            "Your session has timed out. Please sign in again."
        )
        user = None

    if user is None:
        _render_login()
        st.stop()

    _touch_session()
    return user


def _render_login() -> None:
    """The login form, shown in place of whatever page was requested."""
    st.title("Instagram Knowledge Base")

    notice = st.session_state.pop("auth_notice", None)
    if notice:
        st.warning(notice)

    if not has_configured_users():
        st.error("No dashboard accounts are configured yet.")
        st.markdown(
            """
            To create the first account:

            1. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
            2. Run `python scripts/hash_password.py` and follow the prompts
            3. Paste the generated hash into the `[auth.users.*]` section
            4. Restart the application
            """
        )
        return

    st.caption("Please sign in to continue.")

    with st.form("login_form"):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input(
            "Password", type="password", autocomplete="current-password"
        )
        submitted = st.form_submit_button("Sign in", width="stretch")

    if not submitted:
        return

    try:
        user = authenticate(username, password)
    except AppAuthenticationError as exc:
        st.error(exc.user_message)
        return

    st.session_state[SESSION_USER] = user.model_dump()
    _touch_session()
    st.rerun()


def require_admin(user: AppUser, action: str = "This action") -> bool:
    """Show a message and return False when the user is not an admin."""
    if user.is_admin:
        return True
    st.warning(f"{action} requires an administrator account.")
    return False


# -------------------------------------------------------------------- sidebar


def render_sidebar(user: AppUser, config: AppConfig) -> None:
    """Shared sidebar: identity, sign out and a storage hint."""
    with st.sidebar:
        st.markdown(f"**{user.display_name}**")
        st.caption(f"Signed in as {user.username} ({user.role.label})")
        if st.button("Sign out", width="stretch"):
            sign_out()

        st.divider()
        st.caption("Storage")
        st.caption(f"Local: {config.download_dir}")
        st.caption(f"Drive root: {config.drive_root_folder}")


# --------------------------------------------------------------- shared bits


def show_error(exc: Exception) -> None:
    """Show the friendly message; keep the technical detail in the logs."""
    if isinstance(exc, AppError):
        st.error(exc.user_message)
    else:
        st.error("Something went wrong. Please try again.")
    logger.error("UI error: %s: %s", type(exc).__name__, exc)


def status_text(label: str) -> str:
    """Prefix a status with a word, never relying on colour alone."""
    return label


def human_bytes(size: int | None) -> str:
    from models.upload import _human_bytes

    return _human_bytes(int(size or 0))


def caption_preview(caption: str | None, limit: int = 220) -> str:
    """Shorten a caption for a summary line."""
    text = (caption or "").strip()
    if not text:
        return "(no caption)"
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


def open_folder_hint(path: Path | str) -> None:
    """Show a copyable local path.

    A server-side process cannot reliably open a file manager on the user's
    machine, so the honest thing is to show the path rather than pretend.
    """
    st.caption("Local folder")
    st.code(str(path), language=None)


def drive_link(folder_id: str | None) -> str | None:
    if not folder_id:
        return None
    return f"https://drive.google.com/drive/folders/{folder_id}"


def render_summary_metrics(summary: Any) -> None:
    """The History activity strip."""
    columns = st.columns(5)
    columns[0].metric("Total jobs", summary.total)
    columns[1].metric("Completed", summary.completed)
    columns[2].metric("Partial", summary.partial)
    columns[3].metric("Failed", summary.failed)
    columns[4].metric("Local storage", human_bytes(summary.local_bytes))
