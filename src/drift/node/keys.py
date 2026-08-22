"""Local API-key persistence for the headless node bootstrap."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Tuple


def load_or_create_api_key(path: Path) -> Tuple[str, bool]:
    """Load a dedicated secret file or create it atomically with private permissions.

    Native credential-store integration belongs to the desktop milestone. This
    headless bootstrap keeps the secret separate from ordinary JSON/TOML config and
    never writes it to application logs.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    def read_existing() -> Tuple[str, bool]:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"API key path is not a regular file: {path}")
        stored = path.read_text(encoding="utf-8").strip()
        if not stored:
            raise ValueError(f"API key file is empty: {path}")
        return stored, False

    if path.exists() or path.is_symlink():
        return read_existing()

    key = f"drift_{secrets.token_urlsafe(32)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return read_existing()

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise

    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key, True
