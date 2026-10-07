"""Instagram login through a real browser window.

Opens Chrome/Chromium on instagram.com's own login page. The user signs in
there, completing any two-factor or "was this you?" checks with Instagram
directly. Once Instagram sets its session cookie, the cookies are read, the
browser closes, and they are saved as a reusable Instaloader session.

The password is typed into Instagram's page, never into this application, and
is never seen, stored or logged here. The browser runs with a throwaway
profile, so nothing is left behind except the saved session file.

Like the Google consent flow, this needs a display on the machine running the
app: the window opens wherever the Streamlit server runs.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auth.instagram_auth import REQUIRED_SESSION_COOKIES, session_from_cookies
from utils.errors import InstagramAuthenticationError

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.instagram.com/accounts/login/"

#: How long the user has to finish logging in.
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300

#: After the session cookie appears, give Instagram a moment to set the rest.
_SETTLE_SECONDS = 2.0
_POLL_SECONDS = 1.0

#: Browser binaries Playwright can drive without downloading anything.
_SYSTEM_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


class BrowserLoginCancelled(InstagramAuthenticationError):
    user_message = "The browser was closed before the Instagram login finished."


def _instagram_cookies(raw_cookies: list[dict]) -> dict[str, str]:
    """Keep only instagram.com cookies, as a simple name -> value mapping."""
    cookies: dict[str, str] = {}
    for cookie in raw_cookies:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if domain == "instagram.com" or domain.endswith(".instagram.com"):
            cookies[cookie["name"]] = cookie["value"]
    return cookies


def _launch_browser(playwright):
    """Prefer an installed Chrome/Chromium; fall back to Playwright's own."""
    for binary in _SYSTEM_BROWSERS:
        path = shutil.which(binary)
        if path:
            logger.info("Launching browser for Instagram login: %s", binary)
            return playwright.chromium.launch(headless=False, executable_path=path)

    try:
        return playwright.chromium.launch(headless=False)
    except Exception as exc:
        raise InstagramAuthenticationError(
            f"No browser available: {exc}",
            user_message=(
                "No Chrome or Chromium browser was found. Install Google Chrome, "
                "or run `playwright install chromium`, then try again."
            ),
        ) from exc


def _capture_cookies(timeout_seconds: int) -> dict[str, str]:
    """Open the login page and wait for a logged-in session. Runs in a thread."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise InstagramAuthenticationError(
            "Playwright is not installed",
            user_message=(
                "Browser login needs Playwright. Run `pip install -r "
                "requirements.txt` and try again."
            ),
        ) from exc

    closed = threading.Event()

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            # A fresh context is an isolated, in-memory profile.
            context = browser.new_context()
            page = context.new_page()
            page.on("close", lambda _page: closed.set())
            browser.on("disconnected", lambda _browser: closed.set())

            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                raise InstagramAuthenticationError(
                    f"Could not open Instagram login page: {exc}",
                    user_message=(
                        "The browser could not open Instagram. Check your "
                        "internet connection and try again."
                    ),
                ) from exc

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if closed.is_set():
                    raise BrowserLoginCancelled("Login window closed by user")

                try:
                    cookies = _instagram_cookies(context.cookies())
                except PlaywrightError as exc:
                    # The context goes away if the user closes the window.
                    raise BrowserLoginCancelled(
                        f"Browser closed during login: {exc}"
                    ) from exc

                if cookies.get("sessionid"):
                    time.sleep(_SETTLE_SECONDS)
                    try:
                        cookies = _instagram_cookies(context.cookies())
                    except PlaywrightError:
                        pass
                    if all(cookies.get(name) for name in REQUIRED_SESSION_COOKIES):
                        logger.info("Instagram browser login detected")
                        return cookies

                # page.wait_for_timeout keeps Playwright's event loop serviced.
                try:
                    page.wait_for_timeout(_POLL_SECONDS * 1000)
                except PlaywrightError as exc:
                    raise BrowserLoginCancelled(
                        f"Browser closed during login: {exc}"
                    ) from exc

            raise InstagramAuthenticationError(
                "Browser login timed out",
                user_message=(
                    f"The Instagram login was not finished within "
                    f"{timeout_seconds // 60} minutes. Please try again."
                ),
            )
        finally:
            try:
                browser.close()
            except Exception:  # pragma: no cover - already closed
                pass


def login_with_browser(
    session_dir: Path | str,
    *,
    timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
) -> tuple[str, Path]:
    """Open a browser, wait for the user to log in, and save the session.

    Blocks until the login completes, the window is closed, or the timeout
    elapses. Intended to be triggered by an explicit button press.

    Returns:
        ``(username, session_path)``.

    Raises:
        InstagramAuthenticationError: on cancel, timeout, missing browser, or
            a session Instagram does not accept.
    """
    # Playwright's sync API refuses to run on a thread with an asyncio loop,
    # so it gets a dedicated worker thread regardless of the caller.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ig-login") as pool:
        cookies = pool.submit(_capture_cookies, timeout_seconds).result()

    username, path = session_from_cookies(cookies, session_dir)
    logger.info("Saved Instagram session from browser login for user=%s", username)
    return username, path
