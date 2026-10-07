"""Application configuration.

Two distinct sources, deliberately kept apart:

* ``.env`` plus an optional ``data/settings.json`` overlay for operational
  settings. The overlay is what the Settings page writes to, so UI changes
  survive a restart without anyone editing ``.env`` by hand.
* ``.streamlit/secrets.toml`` for credentials: Google OAuth client details and
  dashboard password hashes.

Secrets are read with :mod:`tomllib` rather than ``st.secrets`` so that the
business layer, the tests and ``scripts/`` never need a running Streamlit.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from utils.errors import ConfigurationError

logger = logging.getLogger(__name__)

#: Repository root, i.e. the directory containing ``app.py``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SECRETS_PATH = PROJECT_ROOT / ".streamlit" / "secrets.toml"
SETTINGS_OVERLAY_PATH = PROJECT_ROOT / "data" / "settings.json"

#: Only these settings may be changed from the Settings page.
EDITABLE_SETTINGS = {
    "download_dir",
    "temp_dir",
    "log_level",
    "drive_root_folder",
    "upload_chunk_size",
    "max_upload_retries",
    "cleanup_temp_files",
    "default_destination",
    "organize_by_username",
    "organize_by_year",
    "organize_by_month",
    "organize_by_shortcode",
    "organize_by_date_folder",
}


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r, using %d", key, raw, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class AppConfig(BaseModel):
    """Resolved operational settings."""

    app_env: str = "development"

    # Directories, relative to the project root unless absolute.
    download_dir: str = "downloads"
    temp_dir: str = "temp"
    session_dir: str = "sessions"
    token_dir: str = "tokens"
    data_dir: str = "data"
    log_dir: str = "logs"

    log_level: str = "INFO"

    # Google Drive
    drive_root_folder: str = "Instagram-Knowledge-Base"
    upload_chunk_size: int = Field(default=5 * 1024 * 1024, ge=256 * 1024)
    max_upload_retries: int = Field(default=3, ge=1, le=10)

    # Folder organisation toggles
    organize_by_username: bool = True
    organize_by_year: bool = True
    organize_by_month: bool = True
    organize_by_shortcode: bool = True
    #: One YYYY_MM_DD folder instead of separate year and month folders.
    organize_by_date_folder: bool = False

    # Instagram
    max_instagram_retries: int = Field(default=3, ge=1, le=5)

    # Dashboard login
    session_timeout_minutes: int = Field(default=60, ge=1)
    max_failed_attempts: int = Field(default=5, ge=1)
    lockout_minutes: int = Field(default=15, ge=1)

    # Behaviour
    cleanup_temp_files: bool = False
    default_destination: str = "LOCAL_AND_DRIVE"

    # ----------------------------------------------------------------- paths
    def _resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def download_path(self) -> Path:
        return self._resolve(self.download_dir)

    @property
    def temp_path(self) -> Path:
        return self._resolve(self.temp_dir)

    @property
    def session_path(self) -> Path:
        return self._resolve(self.session_dir)

    @property
    def token_path(self) -> Path:
        return self._resolve(self.token_dir)

    @property
    def data_path(self) -> Path:
        return self._resolve(self.data_dir)

    @property
    def log_path(self) -> Path:
        return self._resolve(self.log_dir)

    @property
    def manifest_db_path(self) -> Path:
        return self.data_path / "manifest.db"

    def ensure_directories(self) -> None:
        """Create the directories the application writes to."""
        for path in (
            self.download_path,
            self.temp_path,
            self.session_path,
            self.token_path,
            self.data_path,
            self.log_path,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _load_dotenv() -> None:
    """Load ``.env`` if python-dotenv is available. Absence is not an error."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _config_from_env() -> AppConfig:
    return AppConfig(
        app_env=_env_str("APP_ENV", "development"),
        download_dir=_env_str("DOWNLOAD_DIR", "downloads"),
        temp_dir=_env_str("TEMP_DIR", "temp"),
        session_dir=_env_str("SESSION_DIR", "sessions"),
        token_dir=_env_str("TOKEN_DIR", "tokens"),
        data_dir=_env_str("DATA_DIR", "data"),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        drive_root_folder=_env_str("DEFAULT_DRIVE_FOLDER", "Instagram-Knowledge-Base"),
        upload_chunk_size=_env_int("UPLOAD_CHUNK_SIZE", 5 * 1024 * 1024),
        max_upload_retries=_env_int("MAX_UPLOAD_RETRIES", 3),
        max_instagram_retries=_env_int("MAX_INSTAGRAM_RETRIES", 3),
        session_timeout_minutes=_env_int("SESSION_TIMEOUT_MINUTES", 60),
        max_failed_attempts=_env_int("MAX_FAILED_LOGIN_ATTEMPTS", 5),
        lockout_minutes=_env_int("LOCKOUT_MINUTES", 15),
        cleanup_temp_files=_env_bool("CLEANUP_TEMP_FILES", False),
    )


def _read_overlay() -> dict[str, Any]:
    if not SETTINGS_OVERLAY_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_OVERLAY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable settings overlay: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in EDITABLE_SETTINGS}


def load_config(*, reload: bool = False) -> AppConfig:
    """Return the effective configuration.

    Order of precedence: ``settings.json`` overlay > ``.env`` > defaults.
    """
    if reload:
        get_config.cache_clear()
    return get_config()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Cached configuration. Call ``load_config(reload=True)`` after saving."""
    _load_dotenv()
    base = _config_from_env()
    overlay = _read_overlay()
    if not overlay:
        return base
    merged = base.model_dump()
    merged.update(overlay)
    try:
        return AppConfig(**merged)
    except Exception as exc:  # invalid overlay should not brick the app
        logger.warning("Settings overlay rejected (%s); using .env values", exc)
        return base


def save_settings(values: dict[str, Any]) -> AppConfig:
    """Persist editable settings to the overlay and return the new config.

    Unknown or non-editable keys are ignored rather than silently written.
    """
    filtered = {k: v for k, v in values.items() if k in EDITABLE_SETTINGS}
    current = _read_overlay()
    current.update(filtered)

    # Validate before writing so a bad value cannot corrupt the overlay.
    candidate = get_config().model_dump()
    candidate.update(current)
    AppConfig(**candidate)

    SETTINGS_OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_OVERLAY_PATH.write_text(
        json.dumps(current, indent=2, sort_keys=True), encoding="utf-8"
    )
    return load_config(reload=True)


# --------------------------------------------------------------------- secrets


@lru_cache(maxsize=1)
def load_secrets() -> dict[str, Any]:
    """Read ``.streamlit/secrets.toml``.

    Returns an empty mapping when the file is absent, so a fresh checkout can
    start up and show setup guidance instead of crashing.
    """
    if not SECRETS_PATH.exists():
        return {}
    try:
        with SECRETS_PATH.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(
            f"Could not parse {SECRETS_PATH.name}: {exc}",
            user_message=(
                "The secrets file could not be read. Please check "
                ".streamlit/secrets.toml for syntax errors."
            ),
        ) from exc


def reload_secrets() -> dict[str, Any]:
    load_secrets.cache_clear()
    return load_secrets()


class DriveAccountConfig(BaseModel):
    """OAuth client details for one Google Drive account.

    ``client_secret`` is held only in memory and must never be logged or shown.
    """

    key: str
    display_name: str
    client_id: str
    client_secret: str

    @property
    def is_usable(self) -> bool:
        return bool(self.client_id) and "YOUR_CLIENT_ID" not in self.client_id


def get_drive_accounts() -> list[DriveAccountConfig]:
    """Configured Google Drive accounts, in declaration order."""
    secrets = load_secrets()
    raw = secrets.get("gdrive") or {}
    accounts: list[DriveAccountConfig] = []
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        accounts.append(
            DriveAccountConfig(
                key=key,
                display_name=entry.get("display_name") or key.title(),
                client_id=entry.get("client_id", ""),
                client_secret=entry.get("client_secret", ""),
            )
        )
    return accounts


def get_drive_account(key: str) -> DriveAccountConfig:
    for account in get_drive_accounts():
        if account.key == key:
            return account
    raise ConfigurationError(
        f"Unknown Google Drive account {key!r}",
        user_message="That Google Drive account is not configured.",
    )


def get_auth_settings() -> dict[str, Any]:
    """Login settings from ``[auth]``, falling back to the app config."""
    config = get_config()
    auth = load_secrets().get("auth") or {}
    return {
        "session_timeout_minutes": int(
            auth.get("session_timeout_minutes", config.session_timeout_minutes)
        ),
        "max_failed_attempts": int(
            auth.get("max_failed_attempts", config.max_failed_attempts)
        ),
        "lockout_minutes": int(auth.get("lockout_minutes", config.lockout_minutes)),
    }


def get_user_records() -> dict[str, dict[str, Any]]:
    """Raw ``[auth.users.*]`` entries, including password hashes.

    Only :mod:`auth.app_auth` should call this.
    """
    auth = load_secrets().get("auth") or {}
    users = auth.get("users") or {}
    return {k: v for k, v in users.items() if isinstance(v, dict)}
