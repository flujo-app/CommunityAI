"""One local Secret Service namespace for every Linux volunteer credential user.

No backend discovery, environment properties, remote bus or configured collection.
Imports and construction do not connect; credential operations still may prompt.
"""

import os
import sys
from functools import lru_cache

from communityai_anchor import linux_anchor as anchor

SERVICE = "org.communityai.desktop.multigpu-volunteer"
ACCOUNT = "multigpu-volunteer-control-v1"


def is_fixed_location(service, account):
    return sys.platform.startswith("linux") and (service, account) == (SERVICE, ACCOUNT)


def protect_credential_memory():
    """Irreversible Linux process protection before materializing a fixed key."""
    import ctypes
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    anchor._require(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    anchor._require(libc.prctl(4, 0, 0, 0, 0) == 0)  # PR_SET_DUMPABLE
    anchor._require(libc.prctl(3, 0, 0, 0, 0) == 0)  # PR_GET_DUMPABLE


@lru_cache(maxsize=1)
def _backend_type():
    import secretstorage
    from jeepney.io.blocking import open_dbus_connection
    from keyring.backends.SecretService import Keyring
    from keyring.errors import KeyringError, KeyringLocked

    class LocalSecretService(Keyring):
        @classmethod
        def _register(cls):
            # Keyring's metaclass invokes this during class creation. This
            # private adapter must never enter generic backend discovery.
            pass

        def __init__(self):
            # KeyringBackend.__init__ applies KEYRING_PROPERTY_* environment
            # overrides. The fixed volunteer namespace intentionally does not.
            pass

        def _query(self, service, username=None, **base):
            anchor._require((service, username) == (SERVICE, ACCOUNT))
            return dict(username=username, service=service, **base)

        def get_preferred_collection(self):
            connection = None
            try:
                runtime = anchor._runtime_directory()
                info = (runtime / "bus").lstat()
                anchor._require(anchor.stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid())
                connection = open_dbus_connection(bus="unix:path=" + str(runtime / "bus"))
                secretstorage.add_match_rules(connection)
                collection = secretstorage.get_default_collection(connection)
                if collection.is_locked():
                    collection.unlock()
                    if collection.is_locked():
                        raise KeyringLocked("Unlock the local Secret Service and retry")
                return collection
            except Exception:
                if connection is not None:
                    connection.close()
                raise KeyringError("The local Secret Service is unavailable or locked") from None

        def _call(self, method, *args):
            try:
                protect_credential_memory()
                return method(self, *args)
            except Exception:
                raise KeyringError("The local Secret Service is unavailable or locked") from None

        def get_password(self, service, username):
            return self._call(Keyring.get_password, service, username)

        def set_password(self, service, username, password):
            return self._call(Keyring.set_password, service, username, password)

        def delete_password(self, service, username):
            return self._call(Keyring.delete_password, service, username)

    return LocalSecretService


def fixed_secret_service():
    return _backend_type()()
