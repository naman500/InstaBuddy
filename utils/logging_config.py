"""Structured logging with secret redaction.

A filter scrubs credential-shaped substrings from every record as a safety net.
The primary defence is simply never passing secrets to the logger, but a belt
and braces approach is warranted where tokens are involved.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_REDACTED = "[REDACTED]"

#: Patterns matched case-insensitively against the formatted message.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key=value / key: value / "key": "value" forms
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|client_secret|token|access_token|"
        r"refresh_token|id_token|api_key|apikey|authorization|session_id|"
        r"password_hash)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"
    ),
    # Bearer tokens
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    # Our own PBKDF2 hash encoding
    re.compile(r"pbkdf2_sha256\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+"),
    # Google OAuth refresh tokens
    re.compile(r"\b1//[A-Za-z0-9._\-]{10,}"),
)


class RedactSecretsFilter(logging.Filter):
    """Replace credential-shaped values in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True

        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

    @staticmethod
    def redact(text: str) -> str:
        result = text
        for index, pattern in enumerate(_PATTERNS):
            if index == 0:
                result = pattern.sub(rf"\1\2{_REDACTED}", result)
            else:
                result = pattern.sub(_REDACTED, result)
        return result


_configured = False


def configure_logging(
    level: str = "INFO",
    log_dir: Path | str | None = None,
    *,
    force: bool = False,
) -> logging.Logger:
    """Configure root logging once, with console and optional rotating file output.

    Streamlit reruns the script constantly, so repeated calls are a no-op unless
    ``force`` is set. Without that guard every rerun would add another handler
    and duplicate every line.
    """
    global _configured

    root = logging.getLogger()
    if _configured and not force:
        root.setLevel(_coerce_level(level))
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(_coerce_level(level))
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    redactor = RedactSecretsFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    if log_dir is not None:
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                directory / "app.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("File logging disabled: %s", exc)

    # Third-party libraries are chatty at INFO; keep them at WARNING.
    for noisy in (
        "urllib3",
        "googleapiclient",
        "googleapiclient.discovery_cache",
        "google_auth_oauthlib",
        "google.auth",
        "instaloader",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return root


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Module-level logger helper."""
    return logging.getLogger(name)
