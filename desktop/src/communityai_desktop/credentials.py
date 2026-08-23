"""Native credential-store adapter for the CommunityAI desktop."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CREDENTIAL_SERVICE = "org.communityai.desktop"
DEFAULT_CREDENTIAL_ACCOUNT = "local-node-control-v1"
DEFAULT_HEADLESS_CONTROL_KEY_PATH = Path.home() / ".drift" / "node" / "control-api.key"
_CONTROL_KEY_RE = re.compile(r"^drift_control_[A-Za-z0-9_-]{43,}$")


class CredentialError(RuntimeError):
    """A control credential could not be loaded or stored safely."""


class CredentialMissingError(CredentialError):
    """The native credential store is usable but has no product control key."""


@dataclass(frozen=True)
class CredentialProvision:
    secret: str
    source: str
    legacy_path: Optional[Path] = None


def _validate_secret(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CredentialError("control credential is empty")
    secret = value.strip()
    if _CONTROL_KEY_RE.fullmatch(secret) is None:
        raise CredentialError("control credential is not a valid drift_control_ key")
    return secret


class NativeCredentialStore:
    """Read and write the privileged node credential through the OS keyring."""

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
        backend = keyring.get_keyring()
        if getattr(backend, "priority", 0) <= 0:
            raise CredentialError("no usable native credential store is available")
        return keyring, KeyringError

    def get(self) -> str:
        keyring, keyring_error = self._keyring()
        try:
            secret = keyring.get_password(self.service, self.account)
        except keyring_error as exc:
            raise CredentialError(f"native credential store failed: {exc}") from exc
        if secret is None:
            raise CredentialMissingError(f"no local-node control credential is stored for account {self.account!r}")
        return _validate_secret(secret)

    def get_or_migrate(self, path: Path | str = DEFAULT_HEADLESS_CONTROL_KEY_PATH) -> str:
        """Use the native store, importing the legacy node file once when necessary."""
        return self.provision(path, allow_create=False).secret

    def provision(
        self,
        path: Path | str = DEFAULT_HEADLESS_CONTROL_KEY_PATH,
        *,
        allow_create: bool = True,
    ) -> CredentialProvision:
        """Return the native key, migrating or creating it when explicitly allowed."""
        try:
            return CredentialProvision(self.get(), "native")
        except CredentialMissingError as missing:
            path = Path(path)
            if path.exists() or path.is_symlink():
                secret = load_private_control_key(path)
                self.set(secret)
                return CredentialProvision(secret, "migrated", path)
            if not allow_create:
                raise CredentialError("CommunityAI is not ready on this computer yet") from missing
            secret = f"drift_control_{secrets.token_urlsafe(32)}"
            self.set(secret)
            return CredentialProvision(secret, "generated")

    def set(self, secret: str) -> None:
        keyring, keyring_error = self._keyring()
        secret = _validate_secret(secret)
        try:
            keyring.set_password(self.service, self.account, secret)
            stored = keyring.get_password(self.service, self.account)
        except keyring_error as exc:
            raise CredentialError(f"native credential store failed: {exc}") from exc
        if stored is None or not secrets.compare_digest(_validate_secret(stored), secret):
            raise CredentialError("native credential store did not retain the control credential")

    def delete(self) -> bool:
        keyring, keyring_error = self._keyring()
        try:
            if keyring.get_password(self.service, self.account) is None:
                return False
            keyring.delete_password(self.service, self.account)
        except keyring_error as exc:
            raise CredentialError(f"native credential store failed: {exc}") from exc
        return True

    @staticmethod
    def retire_legacy_file(provision: CredentialProvision) -> bool:
        """Remove a migrated file only after a native-key node authenticated successfully."""
        path = provision.legacy_path
        if provision.source != "migrated" or path is None:
            return False
        if load_private_control_key(path) != provision.secret:
            raise CredentialError("legacy control credential changed during migration")
        try:
            path.unlink()
        except OSError as exc:
            raise CredentialError("CommunityAI could not retire its migrated local credential") from exc
        return True


def load_private_control_key(path: Path | str) -> str:
    """Read the headless node's migration file without following links."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CredentialError("CommunityAI could not verify its local connection")
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise CredentialError("CommunityAI could not verify its local connection")
        return _validate_secret(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CredentialError("CommunityAI could not open its local connection") from exc
