"""Google Drive page: connect, re-authenticate and test configured accounts.

Access tokens, client IDs and client secrets are never displayed.
"""

from __future__ import annotations

import streamlit as st

from auth import google_auth
from gdrive.client import DriveClient
from utils.config import get_drive_accounts, reload_secrets
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

st.title("Google Drive")
st.caption(
    "Uploads use the Google Drive API with the drive.file scope, which grants "
    "access only to files this application creates."
)

if st.button("Reload configuration"):
    reload_secrets()
    st.rerun()

accounts = get_drive_accounts()

if not accounts:
    st.warning("No Google Drive accounts are configured.")
    st.markdown(
        """
        To add one:

        1. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
        2. Create a Google Cloud project and enable the **Google Drive API**
        3. Configure the OAuth consent screen and create a **Desktop app** OAuth client
        4. Paste the client ID and client secret into a `[gdrive.<name>]` section
        5. Reload configuration and connect the account below

        See README.md for the full walkthrough.
        """
    )
    st.stop()

st.divider()

for account in accounts:
    connected = google_auth.is_connected(config.token_path, account.key)

    with st.container(border=True):
        header = st.columns([3, 1])
        header[0].markdown(f"**{account.display_name}**")
        header[1].caption("Connected" if connected else "Not connected")

        if not account.is_usable:
            st.error(
                "This account is missing its OAuth client details. Please complete "
                "the entry in .streamlit/secrets.toml."
            )
            continue

        buttons = st.columns(3)

        # ---------------------------------------------------------- connect
        connect_label = "Re-authenticate" if connected else "Connect"
        if buttons[0].button(connect_label, key=f"connect_{account.key}"):
            if require_admin(user, "Connecting an account"):
                st.info(
                    "A browser window will open for Google sign-in. Complete it, "
                    "then return here."
                )
                try:
                    with st.spinner("Waiting for Google sign-in..."):
                        google_auth.authorize(config.token_path, account)
                    st.success(f"{account.display_name} is connected.")
                    st.rerun()
                except AppError as exc:
                    show_error(exc)
                except Exception as exc:
                    show_error(exc)

        # ------------------------------------------------------------- test
        if buttons[1].button(
            "Test connection", key=f"test_{account.key}", disabled=not connected
        ):
            try:
                with st.spinner("Contacting Google Drive..."):
                    client = DriveClient(account, config.token_path)
                    info = client.connection_info()

                st.success("Connection is working.")
                detail = st.columns(3)
                detail[0].caption(f"Account name: {info['account_name'] or '-'}")
                detail[1].caption(f"Signed in as: {info['account_email'] or '-'}")
                if info["storage_limit"]:
                    detail[2].caption(
                        f"Storage: {human_bytes(info['storage_used'])} of "
                        f"{human_bytes(info['storage_limit'])}"
                    )
                else:
                    detail[2].caption(
                        f"Storage used: {human_bytes(info['storage_used'])}"
                    )
            except AppError as exc:
                show_error(exc)
            except Exception as exc:
                show_error(exc)

        # ----------------------------------------------------------- forget
        if buttons[2].button(
            "Disconnect", key=f"disconnect_{account.key}", disabled=not connected
        ):
            if require_admin(user, "Disconnecting an account"):
                try:
                    google_auth.disconnect(config.token_path, account.key)
                    st.success(f"{account.display_name} has been disconnected.")
                    st.rerun()
                except AppError as exc:
                    show_error(exc)

st.divider()
st.caption(
    f"Uploads go to the root folder '{config.drive_root_folder}'. "
    f"Chunk size {human_bytes(config.upload_chunk_size)}, "
    f"up to {config.max_upload_retries} retries per file."
)
