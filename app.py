"""Application entry point.

Responsible only for initialisation, the login gate and navigation. All feature
logic lives in ``pages/`` (presentation) and ``services/`` (behaviour).
"""

from __future__ import annotations

import streamlit as st

from utils.ui import bootstrap, render_sidebar, require_login

st.set_page_config(
    page_title="Instagram Knowledge Base",
    page_icon="IG",
    layout="wide",
    initial_sidebar_state="expanded",
)


def main() -> None:
    config = bootstrap()

    # Halts here and renders the login form when there is no valid session, so
    # no page below can run unauthenticated.
    user = require_login()

    render_sidebar(user, config)

    navigation = st.navigation(
        [
            st.Page("pages/1_Download.py", title="Download", default=True),
            st.Page("pages/2_History.py", title="History"),
            st.Page("pages/3_GoogleDrive.py", title="Google Drive"),
            st.Page("pages/4_Settings.py", title="Settings"),
        ]
    )
    navigation.run()


main()
