"""Settings page.

Writes to ``data/settings.json``, which overlays ``.env``. That keeps UI changes
out of source control and makes them survive a restart without anyone editing
environment files by hand.

Credentials are never editable here; they live in ``.streamlit/secrets.toml``.
"""

from __future__ import annotations

import streamlit as st

from auth.browser_login import login_with_browser
from auth.instagram_auth import (
    PendingTwoFactor,
    begin_login,
    check_session,
    complete_two_factor,
    delete_session,
    list_saved_sessions,
    normalize_username,
)
from gdrive.uploader import CHUNK_GRANULARITY, normalize_chunk_size
from models.media import DestinationMode
from utils.config import save_settings
from utils.errors import AppError
from utils.ui import (
    bootstrap,
    current_user,
    human_bytes,
    require_admin,
    require_login,
    show_error,
)

config = bootstrap()
user = require_login() if current_user() is None else current_user()

st.title("Settings")

if not user.is_admin:
    st.warning("Settings are read-only for viewer accounts.")

read_only = not user.is_admin

st.caption(
    "These settings are saved to data/settings.json and override values in .env. "
    "Credentials are configured in .streamlit/secrets.toml and are not editable here."
)

with st.form("settings_form"):
    st.subheader("Storage")
    columns = st.columns(2)
    download_dir = columns[0].text_input(
        "Download directory",
        value=config.download_dir,
        disabled=read_only,
        help="Relative paths are resolved against the project folder.",
    )
    temp_dir = columns[1].text_input(
        "Temporary directory", value=config.temp_dir, disabled=read_only
    )

    st.subheader("Defaults")
    default_columns = st.columns(2)

    modes = list(DestinationMode)
    try:
        current_mode = DestinationMode(config.default_destination)
    except ValueError:
        current_mode = DestinationMode.LOCAL_AND_DRIVE

    default_destination = default_columns[0].selectbox(
        "Default destination",
        options=modes,
        index=modes.index(current_mode),
        format_func=lambda option: option.label,
        disabled=read_only,
    )
    log_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
    log_level = default_columns[1].selectbox(
        "Logging level",
        options=log_levels,
        index=(
            log_levels.index(config.log_level)
            if config.log_level in log_levels
            else 1
        ),
        disabled=read_only,
    )

    st.subheader("Google Drive")
    drive_root_folder = st.text_input(
        "Root folder", value=config.drive_root_folder, disabled=read_only
    )

    st.caption("Folder organisation")
    organize_by_date_folder = st.checkbox(
        "Date folder (YYYY_MM_DD)",
        value=config.organize_by_date_folder,
        disabled=read_only,
        help="One folder per publish date, e.g. Root/2026_09_21/ABC123XYZ. "
        "When on, the Year and Month options below are ignored.",
    )
    toggles = st.columns(4)
    organize_by_username = toggles[0].checkbox(
        "Username", value=config.organize_by_username, disabled=read_only
    )
    organize_by_year = toggles[1].checkbox(
        "Year", value=config.organize_by_year, disabled=read_only
    )
    organize_by_month = toggles[2].checkbox(
        "Month", value=config.organize_by_month, disabled=read_only
    )
    organize_by_shortcode = toggles[3].checkbox(
        "Shortcode", value=config.organize_by_shortcode, disabled=read_only
    )

    upload_columns = st.columns(2)
    chunk_mb = upload_columns[0].number_input(
        "Upload chunk size (MB)",
        min_value=0.25,
        max_value=64.0,
        value=round(config.upload_chunk_size / (1024 * 1024), 2),
        step=0.25,
        disabled=read_only,
        help="Rounded up to the nearest 256 KB, as Google Drive requires.",
    )
    max_upload_retries = upload_columns[1].number_input(
        "Maximum upload retries",
        min_value=1,
        max_value=10,
        value=config.max_upload_retries,
        step=1,
        disabled=read_only,
    )

    st.subheader("Cleanup")
    cleanup_temp_files = st.checkbox(
        "Remove local staging files after a verified upload",
        value=config.cleanup_temp_files,
        disabled=read_only,
        help=(
            "Only applies when the destination is Google Drive only, and only "
            "after every file has uploaded successfully."
        ),
    )

    submitted = st.form_submit_button(
        "Save settings", type="primary", disabled=read_only
    )

if submitted and require_admin(user, "Changing settings"):
    chunk_bytes = normalize_chunk_size(int(chunk_mb * 1024 * 1024))
    try:
        save_settings(
            {
                "download_dir": download_dir.strip() or "downloads",
                "temp_dir": temp_dir.strip() or "temp",
                "log_level": log_level,
                "drive_root_folder": drive_root_folder.strip()
                or "Instagram-Knowledge-Base",
                "upload_chunk_size": chunk_bytes,
                "max_upload_retries": int(max_upload_retries),
                "cleanup_temp_files": bool(cleanup_temp_files),
                "default_destination": default_destination.value,
                "organize_by_username": bool(organize_by_username),
                "organize_by_year": bool(organize_by_year),
                "organize_by_month": bool(organize_by_month),
                "organize_by_shortcode": bool(organize_by_shortcode),
                "organize_by_date_folder": bool(organize_by_date_folder),
            }
        )
        st.success(
            f"Settings saved. Chunk size stored as {human_bytes(chunk_bytes)}."
        )
        st.cache_resource.clear()
        st.rerun()
    except AppError as exc:
        show_error(exc)
    except Exception as exc:
        show_error(exc)

st.divider()

st.subheader("Current effective configuration")
st.caption("Read-only view of resolved paths and limits.")

info_columns = st.columns(2)
with info_columns[0]:
    st.code(
        f"Downloads:  {config.download_path}\n"
        f"Temp:       {config.temp_path}\n"
        f"Sessions:   {config.session_path}\n"
        f"Tokens:     {config.token_path}\n"
        f"Database:   {config.manifest_db_path}\n"
        f"Logs:       {config.log_path}",
        language=None,
    )
with info_columns[1]:
    st.code(
        f"Chunk size:            {human_bytes(config.upload_chunk_size)}\n"
        f"Chunk granularity:     {human_bytes(CHUNK_GRANULARITY)}\n"
        f"Upload retries:        {config.max_upload_retries}\n"
        f"Instagram retries:     {config.max_instagram_retries}\n"
        f"Session timeout:       {config.session_timeout_minutes} min\n"
        f"Lockout after:         {config.max_failed_attempts} failures",
        language=None,
    )

st.divider()
st.subheader("Instagram sessions")
st.caption(
    "Instagram authentication is optional. Log in once here and the session is "
    f"saved to {config.session_path} for reuse on the Download page. Your "
    "password is used for that one login only; it is never stored or logged."
)

STATE_PENDING_2FA = "ig_pending_two_factor"
STATE_IG_FLASH = "ig_session_flash"


def flash(message: str) -> None:
    """Show ``message`` once, after the rerun that follows a change."""
    st.session_state[STATE_IG_FLASH] = message


flash_message = st.session_state.pop(STATE_IG_FLASH, None)
if flash_message:
    st.success(flash_message)

# ------------------------------------------------------------ saved sessions

saved_sessions = list_saved_sessions(config.session_path)

if saved_sessions:
    for session_name in saved_sessions:
        with st.container(border=True):
            row = st.columns([3, 1, 1])
            row[0].markdown(f"**@{session_name}**")

            if row[1].button("Test", key=f"ig_test_{session_name}"):
                with st.spinner("Checking the session with Instagram..."):
                    try:
                        check_session(config.session_path, session_name)
                        st.success(f"The session for @{session_name} is working.")
                    except AppError as exc:
                        show_error(exc)
                    except Exception as exc:
                        show_error(exc)

            if row[2].button(
                "Delete",
                key=f"ig_delete_{session_name}",
                disabled=read_only,
            ):
                if require_admin(user, "Deleting an Instagram session"):
                    try:
                        delete_session(config.session_path, session_name)
                        flash(f"Deleted the session for @{session_name}.")
                        st.rerun()
                    except AppError as exc:
                        show_error(exc)
else:
    st.info("No Instagram sessions saved yet.")

# ------------------------------------------------------------ add a session

pending = st.session_state.get(STATE_PENDING_2FA)

if read_only:
    st.caption("Only administrators can add Instagram sessions.")

elif pending is not None:
    # Second step: the account owner enters their own two-factor code.
    st.markdown(f"**Two-factor code for @{pending.username}**")
    st.caption(
        "Instagram sent a code to your authenticator app or phone. Enter it to "
        "finish logging in."
    )
    with st.form("ig_two_factor_form", clear_on_submit=True):
        code = st.text_input("Verification code", max_chars=12)
        columns = st.columns(2)
        verify = columns[0].form_submit_button("Verify", type="primary")
        cancel = columns[1].form_submit_button("Cancel")

    if cancel:
        st.session_state.pop(STATE_PENDING_2FA, None)
        st.rerun()

    if verify and require_admin(user, "Adding an Instagram session"):
        with st.spinner("Verifying with Instagram..."):
            try:
                complete_two_factor(pending, code, config.session_path)
                st.session_state.pop(STATE_PENDING_2FA, None)
                flash(f"Saved the session for @{pending.username}.")
                st.rerun()
            except AppError as exc:
                show_error(exc)
            except Exception as exc:
                show_error(exc)

else:
    st.markdown("**Log in with a browser** (recommended)")
    st.caption(
        "Opens a Chrome window on Instagram's own login page. Sign in there, "
        "including any two-factor or 'was this you?' checks. As soon as your "
        "feed loads, the window closes and the session is saved here. Your "
        "password goes only to Instagram. The window opens on the computer "
        "running this app; you have 5 minutes."
    )
    if st.button("Open browser and log in", type="primary", key="ig_browser_login"):
        if require_admin(user, "Adding an Instagram session"):
            with st.spinner("Waiting for you to log in to Instagram in the browser..."):
                try:
                    username, _path = login_with_browser(config.session_path)
                    flash(f"Saved the session for @{username}.")
                    st.rerun()
                except AppError as exc:
                    show_error(exc)
                except Exception as exc:
                    show_error(exc)

    with st.expander("Or log in with username and password", expanded=False):
        st.caption(
            "Use an account you own. If Instagram asks you to confirm the login "
            "in its app, approve it there and try again."
        )
        # clear_on_submit wipes the password field from widget state right away.
        with st.form("ig_login_form", clear_on_submit=True):
            ig_username = st.text_input("Instagram username")
            ig_password = st.text_input("Instagram password", type="password")
            login = st.form_submit_button("Log in and save session", type="primary")

        if login and require_admin(user, "Adding an Instagram session"):
            with st.spinner("Logging in to Instagram..."):
                try:
                    outcome = begin_login(
                        ig_username, ig_password, config.session_path
                    )
                    if isinstance(outcome, PendingTwoFactor):
                        st.session_state[STATE_PENDING_2FA] = outcome
                    else:
                        flash(
                            "Saved the session for "
                            f"@{normalize_username(ig_username)}."
                        )
                    st.rerun()
                except AppError as exc:
                    show_error(exc)
                except Exception as exc:
                    show_error(exc)
                finally:
                    del ig_password

    with st.expander("Prefer the command line?", expanded=False):
        st.code("instaloader --login YOUR_USERNAME", language="bash")
        st.caption(f"Then move the session file into {config.session_path}")
