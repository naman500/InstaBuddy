#!/usr/bin/env python3
"""Generate a password hash for ``.streamlit/secrets.toml``.

Usage::

    python scripts/hash_password.py

The password is read with :func:`getpass.getpass`, so it is never echoed to the
terminal and never lands in shell history. Only the resulting hash is printed;
the plaintext is not written anywhere.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

# Allow running this script directly from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth.app_auth import generate_password_hash, suggest_password  # noqa: E402

MIN_LENGTH = 8


def read_password(prompt: str) -> str:
    """Read a password without echoing it.

    ``getpass`` reads from the terminal rather than stdin, which is the secure
    default but blocks when there is no terminal. When stdin is piped we fall
    back to reading it, so the script stays usable in scripted setups.
    """
    if sys.stdin.isatty():
        return getpass.getpass(prompt)

    print(f"{prompt}(reading from stdin; input will not be hidden)")
    line = sys.stdin.readline()
    if not line:
        raise EOFError("no input available")
    return line.rstrip("\n")


def main() -> int:
    print("Dashboard password hash generator")
    print("-" * 34)
    print("The password is not echoed and is never saved to disk.")
    print(f"Suggested strong password: {suggest_password()}")
    print()

    try:
        password = read_password("Password: ")
        confirmation = read_password("Confirm password: ")
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1

    if not password:
        print("Error: password cannot be empty.", file=sys.stderr)
        return 1

    if password != confirmation:
        print("Error: passwords do not match.", file=sys.stderr)
        return 1

    if len(password) < MIN_LENGTH:
        print(
            f"Error: please use at least {MIN_LENGTH} characters.",
            file=sys.stderr,
        )
        return 1

    encoded = generate_password_hash(password)
    del password, confirmation

    print("\nAdd this to .streamlit/secrets.toml:\n")
    print("[auth.users.your_username]")
    print('display_name = "Your Name"')
    print('role = "admin"')
    print(f'password_hash = "{encoded}"')
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
