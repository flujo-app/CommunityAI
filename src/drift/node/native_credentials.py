"""Native credential-store access for desktop-owned local nodes.

This module is deliberately small and imports ``keyring`` lazily. Headless nodes keep
their private-file default; the desktop product opts into this backend explicitly so
the privileged control secret never crosses a command line, environment variable, or
ordinary configuration file.
"""

from __future__ import annotations

from dataclasses import dataclass

from drift.node.keys import validate_control_key

DEFAULT_CREDENTIAL_SERVICE = "org.communityai.desktop"
DEFAULT_CREDENTIAL_ACCOUNT = "local-node-control-v1"


class NativeCredentialError(ValueError):
    """The operating-system credential store could not supply a safe key."""


@dataclass(frozen=True)
class NativeCredentialLocation:
    service: str = DEFAULT_CREDENTIAL_SERVICE
    account: str = DEFAULT_CREDENTIAL_ACCOUNT

    def __post_init__(self) -> None:
        if not isinstance(self.service, str) or not isinstance(self.account, str):
            raise NativeCredentialError("native credential service and account must be strings")
        service, account = self.service.strip(), self.account.strip()
        if not service or not account:
            raise NativeCredentialError("native credential service and account must not be empty")
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "account", account)


def load_native_control_key(location: NativeCredentialLocation = NativeCredentialLocation()) -> str:
    """Load an existing control credential without creating or migrating one."""
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError as exc:
        raise NativeCredentialError("native credential support is not installed; install drift[api]") from exc

    backend = keyring.get_keyring()
    if getattr(backend, "priority", 0) <= 0:
        raise NativeCredentialError("no usable native credential store is available")
    try:
        secret = keyring.get_password(location.service, location.account)
    except KeyringError as exc:
        raise NativeCredentialError(f"native credential store failed: {exc}") from exc
    if secret is None:
        raise NativeCredentialError(
            f"no local-node control credential is stored for native account {location.account!r}"
        )
    try:
        return validate_control_key(secret)
    except ValueError as exc:
        raise NativeCredentialError("native control credential has an invalid key class") from exc
