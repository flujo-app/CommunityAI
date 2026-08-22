"""Native credential-store and private-file adapters for the desktop spike."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DEFAULT_CREDENTIAL_SERVICE = "org.communityai.desktop.spike"
DEFAULT_CREDENTIAL_ACCOUNT = "local-node-control"


class CredentialError(RuntimeError):
    """A control credential could not be loaded or stored safely."""


def _validate_secret(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CredentialError("control credential is empty")
    return value.strip()


class NativeCredentialStore:
    def __init__(
        self,
        service: str = DEFAULT_CREDENTIAL_SERVICE,
        account: str = DEFAULT_CREDENTIAL_ACCOUNT,
    ):
        if not service.strip() or not account.strip():
            raise ValueError("credential service and account must not be empty")
        self.service = service.strip()
        self.account = account.strip()

    @staticmethod
    def _keyring():
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError as exc:
            raise CredentialError("native credential support is not installed") from exc
        return keyring, KeyringError

    def get(self) -> str:
        keyring, keyring_error = self._keyring()
        try:
            secret = keyring.get_password(self.service, self.account)
        except keyring_error as exc:
            raise CredentialError(f"native credential store failed: {exc}") from exc
        if secret is None:
            raise CredentialError(
                f"no control credential is stored for account {self.account!r}; " "run with --store-control-key first"
            )
        return _validate_secret(secret)

    def set(self, secret: str) -> None:
        keyring, keyring_error = self._keyring()
        try:
            keyring.set_password(self.service, self.account, _validate_secret(secret))
        except keyring_error as exc:
            raise CredentialError(f"native credential store failed: {exc}") from exc

    def delete(self) -> bool:
        keyring, keyring_error = self._keyring()
        try:
            if keyring.get_password(self.service, self.account) is None:
                return False
            keyring.delete_password(self.service, self.account)
        except keyring_error as exc:
            raise CredentialError(f"native credential store failed: {exc}") from exc
        return True


def load_private_key_file(path: Path | str) -> str:
    """Load the milestone-4 headless fallback without following links."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CredentialError(f"control-key path is not a regular file: {path}")
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise CredentialError(f"control-key file must not be accessible to group or other users: {path}")
        return _validate_secret(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CredentialError(f"could not read control-key file {path}: {exc}") from exc
