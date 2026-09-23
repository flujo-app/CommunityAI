"""Pinned backend tests with isolated doubles; never connect to an owner bus."""

import stat
import sys
from types import SimpleNamespace

import pytest

from communityai_anchor import linux_secret_service as local

SECRET = "drift_control_" + "a" * 43


@pytest.fixture
def backend(monkeypatch, tmp_path):
    calls = []
    protected = []
    monkeypatch.setattr(local, "protect_credential_memory", lambda: protected.append(True))

    class KeyringError(Exception):
        pass

    class KeyringLocked(KeyringError):
        pass

    class Keyring:
        def __init__(self):
            raise AssertionError("environment-based keyring configuration")

        @property
        def priority(self):
            raise AssertionError("backend probing")

        def get_password(self, service, username):
            self._query(service, username)
            self.get_preferred_collection()
            calls.append("get")
            return SECRET

        def set_password(self, service, username, password):
            self._query(service, username, application="Python keyring library")
            self.get_preferred_collection()
            calls.append("set")

        def delete_password(self, service, username):
            self._query(service, username)
            self.get_preferred_collection()
            calls.append("delete")

    connection = SimpleNamespace(close=lambda: calls.append("close"))
    collection = SimpleNamespace(is_locked=lambda: False, unlock=lambda: calls.append("unlock"))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    bus = runtime / "bus"
    bus.write_bytes(b"fixture, not a real socket")
    original_lstat = type(bus).lstat
    monkeypatch.setattr(
        type(bus),
        "lstat",
        lambda path: SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=123) if path == bus else original_lstat(path),
    )
    monkeypatch.setattr(local.os, "geteuid", lambda: 123, raising=False)
    monkeypatch.setattr(local.anchor, "_runtime_directory", lambda: runtime)

    def connect(*, bus):
        assert protected
        calls.append(("connect", bus))
        return connection

    modules = {
        "secretstorage": SimpleNamespace(
            add_match_rules=lambda conn: calls.append(("matches", conn is connection)),
            get_default_collection=lambda conn: collection,
        ),
        "jeepney.io.blocking": SimpleNamespace(open_dbus_connection=connect),
        "keyring.backends.SecretService": SimpleNamespace(Keyring=Keyring),
        "keyring.errors": SimpleNamespace(KeyringError=KeyringError, KeyringLocked=KeyringLocked),
        "keyring": SimpleNamespace(get_keyring=lambda: pytest.fail("generic backend discovery")),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    local._backend_type.cache_clear()
    yield SimpleNamespace(
        calls=calls, protected=protected, collection=collection, runtime=runtime, error=KeyringError, base=Keyring
    )
    local._backend_type.cache_clear()


def test_constructor_is_lazy_and_operations_ignore_backend_bus_collection_and_scheme_overrides(backend, monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "tcp:host=remote.invalid,port=99")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "untrusted.Keyring")
    monkeypatch.setenv("KEYRING_PROPERTY_preferred_collection", "alternate")
    monkeypatch.setenv("KEYRING_PROPERTY_scheme", "alternate")
    store = local.fixed_secret_service()
    assert backend.calls == []
    assert backend.protected == []
    assert type(store) is type(local.fixed_secret_service())
    assert store._query(local.SERVICE, local.ACCOUNT) == dict(service=local.SERVICE, username=local.ACCOUNT)
    assert store.get_password(local.SERVICE, local.ACCOUNT) == SECRET
    store.set_password(local.SERVICE, local.ACCOUNT, SECRET)
    store.delete_password(local.SERVICE, local.ACCOUNT)
    assert backend.protected == [True, True, True]
    assert [c for c in backend.calls if isinstance(c, tuple) and c[0] == "connect"] == [
        ("connect", "unix:path=" + str(backend.runtime / "bus"))
    ] * 3


@pytest.mark.parametrize("operation", ["get_password", "set_password", "delete_password"])
def test_other_accounts_are_rejected_before_backend_access(backend, operation):
    args = [local.SERVICE, "other-account"] + ([SECRET] if operation == "set_password" else [])
    with pytest.raises(backend.error, match="unavailable or locked"):
        getattr(local.fixed_secret_service(), operation)(*args)
    assert backend.calls == []


def test_locked_store_has_fixed_message_and_closes_failed_connection(backend):
    backend.collection.is_locked = lambda: True
    with pytest.raises(backend.error, match="^The local Secret Service is unavailable or locked$"):
        local.fixed_secret_service().get_password(local.SERVICE, local.ACCOUNT)
    assert "unlock" in backend.calls and backend.calls[-1] == "close"


def test_backend_exception_does_not_escape_with_secret_text(backend):
    def fail(*args):
        raise ValueError(SECRET)

    backend.base.get_password = fail
    with pytest.raises(backend.error) as caught:
        local.fixed_secret_service().get_password(local.SERVICE, local.ACCOUNT)
    assert SECRET not in str(caught.value) and caught.value.__suppress_context__


def test_failed_process_protection_denies_backend_access(backend, monkeypatch):
    def failed():
        raise OSError("fixture protection failure")

    monkeypatch.setattr(local, "protect_credential_memory", failed)
    with pytest.raises(backend.error):
        local.fixed_secret_service().get_password(local.SERVICE, local.ACCOUNT)
    assert backend.calls == []


def test_all_fixed_namespace_readers_use_shared_backend_without_discovery(backend, monkeypatch):
    from communityai_desktop.credentials import NativeCredentialStore

    from communityai_anchor.linux_anchor_credentials import _native_store
    from drift.node.native_credentials import NativeCredentialLocation, load_native_control_key

    monkeypatch.setattr(
        local, "is_fixed_location", lambda service, account: (service, account) == (local.SERVICE, local.ACCOUNT)
    )
    desktop = NativeCredentialStore(local.SERVICE, local.ACCOUNT)
    profile = SimpleNamespace(credential_service=local.SERVICE, credential_account=local.ACCOUNT)
    assert desktop.get() == SECRET
    assert load_native_control_key(NativeCredentialLocation(local.SERVICE, local.ACCOUNT)) == SECRET
    assert _native_store(profile).get() == SECRET
    assert backend.calls.count("get") == 3


def test_missing_lazy_backend_dependency_is_normalized_by_both_product_readers(backend, monkeypatch):
    from communityai_desktop.credentials import CredentialError, NativeCredentialStore

    from drift.node.native_credentials import NativeCredentialError, NativeCredentialLocation, load_native_control_key

    def missing():
        raise ModuleNotFoundError("synthetic missing dependency")

    monkeypatch.setattr(local, "is_fixed_location", lambda *args: True)
    monkeypatch.setattr(local, "fixed_secret_service", missing)
    with pytest.raises(CredentialError, match="support is not installed"):
        NativeCredentialStore(local.SERVICE, local.ACCOUNT).get()
    with pytest.raises(NativeCredentialError, match="support is not installed"):
        load_native_control_key(NativeCredentialLocation(local.SERVICE, local.ACCOUNT))
    assert backend.calls == []


def test_profile_constants_cannot_silently_drift_from_shared_backend(tmp_path):
    from communityai_desktop.profiles import VolunteerProfile

    profile = VolunteerProfile(tmp_path)
    assert (profile.credential_service, profile.credential_account) == (local.SERVICE, local.ACCOUNT)
