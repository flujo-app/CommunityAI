from __future__ import annotations

import pytest

keyring = pytest.importorskip("keyring")

from drift.node.native_credentials import NativeCredentialError, NativeCredentialLocation, load_native_control_key

CONTROL_KEY = "drift_control_" + "N" * 43


class FakeBackend:
    priority = 1


def test_native_node_loads_the_desktop_credential(monkeypatch):
    calls = []
    monkeypatch.setattr(keyring, "get_keyring", lambda: FakeBackend())
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, account: calls.append((service, account)) or CONTROL_KEY,
    )

    assert load_native_control_key(NativeCredentialLocation(" service ", " account ")) == CONTROL_KEY
    assert calls == [("service", "account")]


@pytest.mark.parametrize("secret", [None, "drift_client", "drift_control_short"])
def test_native_node_fails_closed_on_missing_or_wrong_key_class(monkeypatch, secret):
    monkeypatch.setattr(keyring, "get_keyring", lambda: FakeBackend())
    monkeypatch.setattr(keyring, "get_password", lambda service, account: secret)

    with pytest.raises(NativeCredentialError):
        load_native_control_key()
