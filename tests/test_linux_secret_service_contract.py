"""Locked SecretService close contract without a real bus or credential store."""

from types import SimpleNamespace

import pytest

from communityai_anchor import linux_secret_service as local

keyring = pytest.importorskip("keyring")
keyring_backend = pytest.importorskip("keyring.backend")
secret_service = pytest.importorskip("keyring.backends.SecretService")
pytest.importorskip("secretstorage")
blocking = pytest.importorskip("jeepney.io.blocking")


SECRET = "drift_control_" + "c" * 43


class FakeConnection:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


class FakeItem:
    def __init__(self, *, fail_delete=False):
        self.fail_delete = fail_delete
        self.deleted = False

    @staticmethod
    def is_locked():
        return False

    @staticmethod
    def get_secret():
        return SECRET.encode()

    def delete(self):
        if self.fail_delete:
            raise RuntimeError("synthetic delete failure")
        self.deleted = True
        return True


class FakeCollection:
    def __init__(self, *, failure=None):
        self.connection = FakeConnection()
        self.failure = failure
        self.item = FakeItem(fail_delete=failure == "delete")
        self.created = None

    def search_items(self, attributes):
        assert attributes == {"service": local.SERVICE, "username": local.ACCOUNT}
        if self.failure == "search":
            raise RuntimeError("synthetic search failure")
        return [self.item]

    def create_item(self, label, attributes, password, replace):
        if self.failure == "create":
            raise RuntimeError("synthetic create failure")
        self.created = (label, attributes, password, replace)


@pytest.fixture
def store(monkeypatch):
    bus_calls, discovery_calls, protected = [], [], []

    def forbidden_bus(*args, **kwargs):
        bus_calls.append((args, kwargs))
        raise AssertionError("a unit contract attempted a real bus connection")

    def forbidden_discovery(*args, **kwargs):
        discovery_calls.append((args, kwargs))
        raise AssertionError("the fixed backend attempted generic discovery")

    monkeypatch.setattr(blocking, "open_dbus_connection", forbidden_bus)
    monkeypatch.setattr(keyring, "get_keyring", forbidden_discovery)
    monkeypatch.setattr(local, "protect_credential_memory", lambda: protected.append(True))
    local._backend_type.cache_clear()
    result = local.fixed_secret_service()
    assert isinstance(result, secret_service.Keyring)
    assert type(result) not in keyring_backend.KeyringBackend._classes
    assert bus_calls == discovery_calls == protected == []
    yield SimpleNamespace(
        backend=result,
        bus_calls=bus_calls,
        discovery_calls=discovery_calls,
        protected=protected,
    )
    local._backend_type.cache_clear()


def test_constructor_is_lazy_and_never_uses_generic_discovery(store):
    assert store.bus_calls == []
    assert store.discovery_calls == []
    assert store.protected == []


def test_locked_keyring_success_paths_close_every_collection_connection(store, monkeypatch):
    collections = [FakeCollection(), FakeCollection(), FakeCollection()]
    pending = iter(collections)
    monkeypatch.setattr(store.backend, "get_preferred_collection", lambda: next(pending))

    assert store.backend.get_password(local.SERVICE, local.ACCOUNT) == SECRET
    store.backend.set_password(local.SERVICE, local.ACCOUNT, SECRET)
    assert store.backend.delete_password(local.SERVICE, local.ACCOUNT) is True

    assert [collection.connection.close_count for collection in collections] == [1, 1, 1]
    assert collections[1].created == (
        "Password for 'multigpu-volunteer-control-v1' on 'org.communityai.desktop.multigpu-volunteer'",
        {
            "application": "Python keyring library",
            "service": local.SERVICE,
            "username": local.ACCOUNT,
        },
        SECRET,
        True,
    )
    assert collections[2].item.deleted
    assert store.protected == [True, True, True]
    assert store.bus_calls == store.discovery_calls == []


@pytest.mark.parametrize(
    "operation,failure",
    [("get", "search"), ("set", "create"), ("delete", "delete")],
)
def test_locked_keyring_error_paths_close_collection_connection(store, monkeypatch, operation, failure):
    collection = FakeCollection(failure=failure)
    monkeypatch.setattr(store.backend, "get_preferred_collection", lambda: collection)

    with pytest.raises(
        keyring.errors.KeyringError, match="^The local Secret Service is unavailable or locked$"
    ) as caught:
        if operation == "get":
            store.backend.get_password(local.SERVICE, local.ACCOUNT)
        elif operation == "set":
            store.backend.set_password(local.SERVICE, local.ACCOUNT, SECRET)
        else:
            store.backend.delete_password(local.SERVICE, local.ACCOUNT)

    assert caught.value.__suppress_context__
    assert collection.connection.close_count == 1
    assert store.protected == [True]
    assert store.bus_calls == store.discovery_calls == []
