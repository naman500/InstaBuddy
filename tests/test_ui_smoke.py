"""Streamlit smoke tests using the official AppTest harness.

These run the real page scripts in-process and fail on any uncaught exception,
which is what catches the class of bug that a plain import check misses.

No network access: Instagram and Google Drive are never contacted because no
analyze or upload button is pressed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from auth import app_auth
from auth.app_auth import hash_password
from utils import config as config_module

PASSWORD = "dashboard-password"
TEST_ITERATIONS = 1_000

#: AppTest resolves relative paths against the calling file, so scripts are
#: referenced absolutely from the project root.
ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")

PAGES = [
    str(ROOT / "pages" / name)
    for name in (
        "1_Download.py",
        "2_History.py",
        "3_GoogleDrive.py",
        "4_Settings.py",
    )
]


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the app at temp directories and a temp secrets file."""
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(
        f"""
[auth]
session_timeout_minutes = 60
max_failed_attempts = 3
lockout_minutes = 15

[auth.users.admin]
display_name = "Administrator"
role = "admin"
password_hash = "{hash_password(PASSWORD, iterations=TEST_ITERATIONS)}"

[auth.users.reader]
display_name = "Reader"
role = "viewer"
password_hash = "{hash_password(PASSWORD, iterations=TEST_ITERATIONS)}"

[gdrive.personal]
display_name = "Personal Google Drive"
client_id = "cid.apps.googleusercontent.com"
client_secret = "secret"
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(config_module, "SECRETS_PATH", secrets_path)
    monkeypatch.setattr(
        config_module, "SETTINGS_OVERLAY_PATH", tmp_path / "settings.json"
    )
    for name, value in {
        "DOWNLOAD_DIR": tmp_path / "downloads",
        "TEMP_DIR": tmp_path / "temp",
        "SESSION_DIR": tmp_path / "sessions",
        "TOKEN_DIR": tmp_path / "tokens",
        "DATA_DIR": tmp_path / "data",
    }.items():
        monkeypatch.setenv(name, str(value))

    config_module.get_config.cache_clear()
    config_module.load_secrets.cache_clear()
    app_auth.reset_tracker()

    import streamlit as st

    st.cache_resource.clear()

    yield tmp_path

    config_module.get_config.cache_clear()
    config_module.load_secrets.cache_clear()
    app_auth.reset_tracker()
    st.cache_resource.clear()


def signed_in(path: str, *, role: str = "admin") -> AppTest:
    """An AppTest for ``path`` with an authenticated session preloaded."""
    from datetime import datetime, timezone

    app = AppTest.from_file(path, default_timeout=60)
    app.session_state["auth_user"] = {
        "username": "admin" if role == "admin" else "reader",
        "display_name": "Administrator" if role == "admin" else "Reader",
        "role": role,
    }
    app.session_state["auth_last_seen"] = datetime.now(timezone.utc)
    return app


def assert_clean(app: AppTest) -> None:
    messages = [str(e.value) for e in app.exception]
    assert not messages, f"page raised: {messages}"


# --------------------------------------------------------------------- gating


def test_fresh_install_shows_setup_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no accounts configured the app explains how to create one."""
    monkeypatch.setattr(config_module, "SECRETS_PATH", tmp_path / "missing.toml")
    monkeypatch.setattr(
        config_module, "SETTINGS_OVERLAY_PATH", tmp_path / "settings.json"
    )
    config_module.get_config.cache_clear()
    config_module.load_secrets.cache_clear()

    app = AppTest.from_file(APP, default_timeout=60).run()

    assert_clean(app)
    assert app.title[0].value == "Instagram Knowledge Base"
    assert any("No dashboard accounts" in e.value for e in app.error)
    assert any("hash_password.py" in m.value for m in app.markdown)


def test_login_form_shown_when_not_authenticated(isolated_app: Path) -> None:
    app = AppTest.from_file(APP, default_timeout=60).run()

    assert_clean(app)
    assert app.title[0].value == "Instagram Knowledge Base"
    labels = [widget.label for widget in app.text_input]
    assert "Username" in labels
    assert "Password" in labels
    # The Download page must not have rendered behind the gate.
    assert not any("Download Instagram content" in t.value for t in app.title)


def test_wrong_password_shows_generic_error(isolated_app: Path) -> None:
    app = AppTest.from_file(APP, default_timeout=60).run()

    app.text_input[0].set_value("admin")
    app.text_input[1].set_value("not-the-password")
    app.button[0].click().run()

    assert_clean(app)
    assert any(
        e.value == "Incorrect username or password." for e in app.error
    ), [e.value for e in app.error]


def test_unknown_user_message_matches_wrong_password(isolated_app: Path) -> None:
    app = AppTest.from_file(APP, default_timeout=60).run()
    app.text_input[0].set_value("ghost")
    app.text_input[1].set_value("whatever")
    app.button[0].click().run()

    assert any(e.value == "Incorrect username or password." for e in app.error)


def test_successful_login_reaches_download_page(isolated_app: Path) -> None:
    app = AppTest.from_file(APP, default_timeout=60).run()

    app.text_input[0].set_value("admin")
    app.text_input[1].set_value(PASSWORD)
    app.button[0].click().run()

    assert_clean(app)
    assert app.session_state["auth_user"]["username"] == "admin"
    titles = [t.value for t in app.title]
    assert "Download Instagram content" in titles


def test_expired_session_is_rejected(isolated_app: Path) -> None:
    from datetime import datetime, timedelta, timezone

    app = AppTest.from_file(APP, default_timeout=60)
    app.session_state["auth_user"] = {
        "username": "admin",
        "display_name": "Administrator",
        "role": "admin",
    }
    app.session_state["auth_last_seen"] = datetime.now(timezone.utc) - timedelta(
        hours=5
    )
    app.run()

    assert_clean(app)
    assert "auth_user" not in app.session_state
    assert any("timed out" in w.value for w in app.warning)


def test_authenticated_session_renders_navigation(isolated_app: Path) -> None:
    from datetime import datetime, timezone

    app = AppTest.from_file(APP, default_timeout=60)
    app.session_state["auth_user"] = {
        "username": "admin",
        "display_name": "Administrator",
        "role": "admin",
    }
    app.session_state["auth_last_seen"] = datetime.now(timezone.utc)
    app.run()

    assert_clean(app)
    assert "Download Instagram content" in [t.value for t in app.title]
    sidebar_text = " ".join(m.value for m in app.sidebar.markdown)
    assert "Administrator" in sidebar_text


# ----------------------------------------------------------------- every page


@pytest.mark.parametrize("page", PAGES, ids=lambda p: Path(p).stem)
def test_page_renders_for_admin(isolated_app: Path, page: str) -> None:
    app = signed_in(page).run()
    assert_clean(app)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: Path(p).stem)
def test_page_renders_for_viewer(isolated_app: Path, page: str) -> None:
    app = signed_in(page, role="viewer").run()
    assert_clean(app)


# -------------------------------------------------------------- page specifics


def test_download_page_sections(isolated_app: Path) -> None:
    app = signed_in(PAGES[0]).run()
    assert_clean(app)

    headers = " ".join(h.value for h in app.subheader)
    assert "1. Instagram URL" in headers

    labels = [w.label for w in app.text_input]
    assert "Instagram URL" in labels


def test_download_page_defaults_to_public_instagram_mode(
    isolated_app: Path,
) -> None:
    """Public, no-login access must be the default."""
    app = signed_in(PAGES[0]).run()
    assert_clean(app)

    auth_radio = next(
        (r for r in app.radio if r.label == "Access mode"), None
    )
    assert auth_radio is not None
    assert auth_radio.value == "Public content / no login"


def test_analyze_disabled_without_a_url(isolated_app: Path) -> None:
    app = signed_in(PAGES[0]).run()
    analyze = next((b for b in app.button if b.label == "Analyze"), None)
    assert analyze is not None
    assert analyze.disabled is True


def test_invalid_url_shows_friendly_message(isolated_app: Path) -> None:
    """Validation happens before any network call, so this is safe offline."""
    app = signed_in(PAGES[0])
    app.run()

    url_input = next(w for w in app.text_input if w.label == "Instagram URL")
    url_input.set_value("https://example.com/not-instagram").run()

    analyze = next(b for b in app.button if b.label == "Analyze")
    analyze.click().run()

    assert_clean(app)
    assert any(
        "valid Instagram post" in e.value for e in app.error
    ), [e.value for e in app.error]


def test_history_page_empty_state(isolated_app: Path) -> None:
    app = signed_in(PAGES[1]).run()
    assert_clean(app)
    assert any("No jobs have run yet" in i.value for i in app.info)


def test_gdrive_page_lists_configured_account(isolated_app: Path) -> None:
    app = signed_in(PAGES[2]).run()
    assert_clean(app)

    text = " ".join(m.value for m in app.markdown)
    assert "Personal Google Drive" in text


def test_gdrive_page_never_shows_credentials(isolated_app: Path) -> None:
    app = signed_in(PAGES[2]).run()
    assert_clean(app)

    rendered = " ".join(
        [m.value for m in app.markdown]
        + [c.value for c in app.caption]
        + [c.value for c in app.code]
    )
    assert "secret" not in rendered
    assert "cid.apps.googleusercontent.com" not in rendered


def test_settings_page_is_read_only_for_viewer(isolated_app: Path) -> None:
    app = signed_in(PAGES[3], role="viewer").run()
    assert_clean(app)
    assert any("read-only for viewer" in w.value for w in app.warning)


def test_settings_page_editable_for_admin(isolated_app: Path) -> None:
    app = signed_in(PAGES[3]).run()
    assert_clean(app)
    assert not any("read-only for viewer" in w.value for w in app.warning)

    labels = [w.label for w in app.text_input]
    assert "Download directory" in labels
    assert "Root folder" in labels


def test_settings_save_persists_overlay(isolated_app: Path) -> None:
    app = signed_in(PAGES[3]).run()

    root = next(w for w in app.text_input if w.label == "Root folder")
    root.set_value("My-Archive")

    submit = next(b for b in app.button if b.label == "Save settings")
    submit.click().run()

    assert_clean(app)
    overlay = config_module.SETTINGS_OVERLAY_PATH
    assert overlay.is_file()
    import json

    saved = json.loads(overlay.read_text(encoding="utf-8"))
    assert saved["drive_root_folder"] == "My-Archive"


def test_url_list_mode_checks_links(isolated_app: Path) -> None:
    """The URL-list mode parses pasted links locally and offers the batch run."""
    app = signed_in(str(ROOT / "pages" / "1_Download.py"))
    app.run()
    assert_clean(app)

    app.radio(key="download_mode").set_value("List of URLs (batch)").run()
    assert_clean(app)

    app.text_area[0].input(
        "https://www.instagram.com/reel/AAAAA11111/\n"
        "https://www.instagram.com/p/AAAAA11111/\n"
        "https://www.instagram.com/p/BBBBB22222/\n"
        "https://www.instagram.com/nasa/"
    ).run()
    next(b for b in app.button if b.label == "Check URLs").click().run()
    assert_clean(app)

    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Valid links"] == "2"
    assert metrics["Duplicates removed"] == "1"
    assert metrics["Invalid"] == "1"
    assert any(b.label == "Download all" for b in app.button)
