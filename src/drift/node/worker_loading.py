"""Private cooperative loading exclusion and generation-bound acknowledgements.

This protocol never releases a memory reservation. Callers must retain summed
staging claims and certify process cleanup before removing generation files.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

LOADING_ENV_PREFIX = "DRIFT_INTERNAL_LOADING_"
LOADING_ENV_KEYS = tuple(LOADING_ENV_PREFIX + suffix for suffix in ("DIR", "TOKEN", "NONCE", "DIGEST"))
WORKER_LOADING_FAILED_EXIT_CODE = 74
_ERROR = "private worker loading protocol is unavailable or invalid"
_MAX_JSON = 8192
_STATES = {"waiting", "loading", "ready", "failed", "memory_rejected"}


class LoadingProtocolError(RuntimeError):
    def __init__(self):
        super().__init__(_ERROR)


class _FileReplaced(RuntimeError):
    """An atomic path replacement, distinct from mutation of the opened file."""


def _require(condition):
    if not condition:
        raise LoadingProtocolError()


def _hex(value, size=64):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{%d}" % size, value) is not None


def _digest(value):
    return isinstance(value, str) and value.startswith("sha256:") and _hex(value[7:])


def _identity(info):
    return info.st_dev, info.st_ino


def _fingerprint(info):
    return (*_identity(info), info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _same_opened(path_info, opened):
    left, right = _fingerprint(path_info), _fingerprint(opened)
    # Windows lstat ctime can be creation time while fstat ctime is change time.
    return left[:5] + left[6:] == right[:5] + right[6:] if os.name == "nt" else left == right


def _stat(path, *, directory=False, private=True):
    info = os.lstat(path)
    _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400)
    _require(bool(info.st_ino) and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))
    if not directory:
        _require(info.st_nlink == 1)
    if private and os.name != "nt":
        _require(not info.st_mode & 0o077 and info.st_uid == os.geteuid())
    return info


def _directory(path, *, private=True):
    raw = os.fspath(path)
    _require(isinstance(raw, str) and 0 < len(raw) <= 4096 and "\0" not in raw and os.path.isabs(raw))
    normalized = os.path.normcase(os.path.normpath(raw))
    root = Path(normalized)
    for parent in (*reversed(root.parents), root):
        _stat(parent, directory=True, private=False)
    _require(os.path.normcase(os.path.realpath(root)) == normalized)
    _stat(root, directory=True, private=private)
    return root


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _encode(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    _require(len(payload) <= _MAX_JSON)
    return payload


def _read(path):
    before = _stat(path)
    _require(0 < before.st_size <= _MAX_JSON)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise _FileReplaced()
        _require(_same_opened(before, opened))
        payload = bytearray()
        while len(payload) <= _MAX_JSON:
            chunk = os.read(descriptor, min(4096, _MAX_JSON + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        _require(len(payload) == before.st_size)
        _require(_fingerprint(os.fstat(descriptor)) == _fingerprint(opened))
        after = _stat(path)
        if _identity(after) != _identity(before):
            raise _FileReplaced()
        _require(_fingerprint(after) == _fingerprint(before))
    finally:
        os.close(descriptor)
    value = json.loads(
        payload, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(LoadingProtocolError())
    )
    _require(isinstance(value, dict))
    return value


def _exclusive(path, value):
    """Immutable descriptor/owner publication; nobody consumes it before return."""
    payload = _encode(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            _require(written > 0)
            offset += written
        os.fsync(descriptor)
        _require(_same_opened(_stat(path), os.fstat(descriptor)))
    finally:
        os.close(descriptor)


def _replace(path, value):
    payload = _encode(value)
    try:
        before = _fingerprint(_stat(path))
    except FileNotFoundError:
        before = None
    descriptor, temporary = tempfile.mkstemp(prefix=".loading-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current = _fingerprint(_stat(path))
        except FileNotFoundError:
            current = None
        _require(current == before)
        # Windows readers can momentarily deny delete-sharing. This is not a
        # changed generation: retry the same validated destination, boundedly.
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 19:
                    raise
                time.sleep(0.01)
                try:
                    observed = _fingerprint(_stat(path))
                except FileNotFoundError:
                    observed = None
                _require(observed == before)
        temporary = None
        _stat(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            os.unlink(temporary)


def _gate(root, *, initialize=False):
    root = Path(root)
    if initialize:
        _directory(root.parent, private=False)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
    root = _directory(root)
    lock, marker = root / "loading.lock", root / "loading-gate.json"
    if initialize:
        try:
            _stat(marker)
        except FileNotFoundError:
            # Only the exclusive first lock creator may establish its marker.
            # A pre-existing lock without a marker never authorizes a reset.
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _require(os.write(descriptor, b"\0") == 1)
                os.fsync(descriptor)
                opened = os.fstat(descriptor)
                _require(_same_opened(_stat(lock), opened))
                _exclusive(marker, {"schema_version": 1, "lock_identity": list(_identity(opened))})
            finally:
                os.close(descriptor)
    value = _read(marker)
    _require(
        set(value) == {"schema_version", "lock_identity"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
    )
    identity = value["lock_identity"]
    _require(isinstance(identity, list) and len(identity) == 2 and all(type(x) is int and x >= 0 for x in identity))
    info = _stat(lock)
    _require(info.st_size == 1 and list(_identity(info)) == identity)
    return root, tuple(identity)


def _cancel(cancelled):
    if cancelled is not None:
        _require(callable(cancelled) and not cancelled())


class _GateLock:
    def __init__(self, root, identity, cancelled):
        self.root, self.identity, self.cancelled = root, identity, cancelled
        self.descriptor = None
        self.locked = False

    def acquire(self):
        _cancel(self.cancelled)
        self.descriptor = os.open(self.root / "loading.lock", os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.set_inheritable(self.descriptor, False)
            opened = os.fstat(self.descriptor)
            _require(_identity(opened) == self.identity and _same_opened(_stat(self.root / "loading.lock"), opened))
            while True:
                _cancel(self.cancelled)
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(self.descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.locked = True
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    time.sleep(0.05)
            _cancel(self.cancelled)
            _require(_gate(self.root)[1] == self.identity)
            _require(_identity(os.fstat(self.descriptor)) == self.identity)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.descriptor is None:
            return
        descriptor, self.descriptor = self.descriptor, None
        try:
            if self.locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            self.locked = False
            os.close(descriptor)


@contextmanager
def loading_gate(directory, *, cancelled=None):
    """Serialize synchronous parent metadata work; no claim or status is created."""
    gate = None
    try:
        _cancel(cancelled)
        root, identity = _gate(directory, initialize=True)
        gate = _GateLock(root, identity, cancelled)
        gate.acquire()
        yield
    except Exception:
        raise LoadingProtocolError() from None
    finally:
        if gate is not None:
            try:
                gate.close()
            except Exception:
                raise LoadingProtocolError() from None


def initialize_loading_gate(directory):
    """Create/validate the gate without acquiring it; serialize first creation."""
    try:
        _gate(directory, initialize=True)
    except Exception:
        raise LoadingProtocolError() from None


@dataclass(frozen=True)
class LoadingBinding:
    directory: str = field(repr=False)
    token: str = field(repr=False)
    nonce: str = field(repr=False)
    binding_digest: str = field(repr=False)

    def __post_init__(self):
        _require(
            isinstance(self.directory, str)
            and 0 < len(self.directory) <= 4096
            and os.path.isabs(self.directory)
            and "\0" not in self.directory
        )
        _require(isinstance(self.token, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.token) is not None)
        _require(_hex(self.nonce, 32) and _digest(self.binding_digest))

    def environment(self):
        return dict(zip(LOADING_ENV_KEYS, (self.directory, self.token, self.nonce, self.binding_digest)))

    def _payload(self):
        return {"schema_version": 1, "token": self.token, "nonce": self.nonce, "binding_digest": self.binding_digest}

    def _path(self, kind):
        return Path(self.directory) / (self.nonce + "." + kind + ".json")


def loading_claim_digest(*, manifest_digest, block_indices, artifact_bytes, artifact_set_digest, cache_root):
    try:
        _require(_digest(manifest_digest) and _hex(artifact_set_digest))
        _require(
            isinstance(block_indices, str)
            and re.fullmatch(r"(?:0|[1-9][0-9]{0,2}):[1-9][0-9]{0,2}", block_indices) is not None
        )
        start, end = map(int, block_indices.split(":"))
        _require(0 <= start < end <= 512 and type(artifact_bytes) is int and 0 < artifact_bytes <= 2**63 - 1)
        root = _directory(cache_root, private=False)
        payload = dict(
            schema_version=1,
            manifest_digest=manifest_digest,
            block_indices=block_indices,
            artifact_bytes=artifact_bytes,
            artifact_set_digest=artifact_set_digest,
            cache_root=str(root),
        )
        return "sha256:" + hashlib.sha256(_encode(payload)).hexdigest()
    except Exception:
        raise LoadingProtocolError() from None


def _validate_binding(binding):
    _require(isinstance(binding, LoadingBinding))
    binding.__post_init__()
    root, identity = _gate(binding.directory)
    _require(str(root) == binding.directory)
    descriptor = _read(binding._path("binding"))
    for key in ("directory_identity", "lock_identity"):
        value = descriptor.get(key)
        _require(isinstance(value, list) and len(value) == 2 and all(type(item) is int for item in value))
    expected = {
        **binding._payload(),
        "directory_identity": list(_identity(_stat(root, directory=True))),
        "lock_identity": list(identity),
    }
    _require(descriptor == expected and type(descriptor.get("schema_version")) is int)
    return root, identity


def create_loading_binding(directory, token, binding_digest):
    try:
        # Validate untrusted scalar fields before touching the filesystem.
        draft = LoadingBinding(str(directory), token, uuid4().hex, binding_digest)
        root, identity = _gate(draft.directory, initialize=True)
        binding = LoadingBinding(str(root), token, draft.nonce, binding_digest)
        _exclusive(
            binding._path("binding"),
            {
                **binding._payload(),
                "directory_identity": list(_identity(_stat(root, directory=True))),
                "lock_identity": list(identity),
            },
        )
        _validate_binding(binding)
        return binding
    except Exception:
        raise LoadingProtocolError() from None


def _status(binding, value, expected_pid):
    expected = binding._payload()
    _require(set(value) == {*expected, "pid", "state"})
    _require(all(value[key] == item for key, item in expected.items()) and type(value["schema_version"]) is int)
    _require(type(value["pid"]) is int and value["pid"] == expected_pid and value["state"] in _STATES)
    owner = _read(binding._path("owner"))
    _require(
        owner == {**expected, "pid": expected_pid}
        and type(owner.get("pid")) is int
        and type(owner.get("schema_version")) is int
    )
    return value["state"]


def read_loading_status(binding, *, expected_pid):
    try:
        _require(type(expected_pid) is int and 0 < expected_pid <= 2**63 - 1)
        _validate_binding(binding)
        for attempt in range(8):
            try:
                value = _read(binding._path("status"))
                break
            except FileNotFoundError:
                return None
            except _FileReplaced:
                if attempt == 7:
                    raise
        return _status(binding, value, expected_pid)
    except Exception:
        raise LoadingProtocolError() from None


def cleanup_loading_binding(binding):
    """Caller certifies process-tree cleanup; never remove/reset the stable gate."""
    try:
        _require(isinstance(binding, LoadingBinding))
        binding.__post_init__()
        _gate(binding.directory)
        try:
            _stat(binding._path("binding"))
        except FileNotFoundError:
            # Known-owner cleanup can precede an uncertain journal removal.
            # Retry only a wholly removed generation; never reset partial state.
            for kind in ("status", "owner"):
                try:
                    _stat(binding._path(kind))
                except FileNotFoundError:
                    continue
                raise LoadingProtocolError()
            return
        _validate_binding(binding)
        for kind in ("status", "owner", "binding"):
            path = binding._path(kind)
            try:
                info = _stat(path)
            except FileNotFoundError:
                continue
            value = _read(path)
            _require(all(value.get(key) == item for key, item in binding._payload().items()))
            _require(_fingerprint(_stat(path)) == _fingerprint(info))
            path.unlink()
    except Exception:
        raise LoadingProtocolError() from None


class LoadingSession:
    def __init__(self, binding, cancelled):
        self.binding = binding
        self.cancelled = cancelled
        self.gate = None
        self.state = None
        self.pid = os.getpid()

    def _publish(self, state):
        _validate_binding(self.binding)
        _replace(self.binding._path("status"), {**self.binding._payload(), "pid": self.pid, "state": state})
        self.state = state

    def __enter__(self):
        try:
            _require(self.state is None and self.pid == os.getpid())
            root, identity = _validate_binding(self.binding)
            _exclusive(self.binding._path("owner"), {**self.binding._payload(), "pid": self.pid})
            self._publish("waiting")
            self.gate = _GateLock(root, identity, self.cancelled)
            self.gate.acquire()
            self._publish("loading")
            return self
        except BaseException:
            try:
                if self.state is not None:
                    self.fail()
            finally:
                raise LoadingProtocolError() from None

    def ready(self):
        try:
            _require(self.state == "loading" and self.gate is not None and self.gate.locked and self.pid == os.getpid())
            _cancel(self.cancelled)
            self._publish("ready")
            self.gate.close()
        except Exception:
            raise LoadingProtocolError() from None

    def fail(self):
        self._fail("failed")

    def fail_memory(self):
        self._fail("memory_rejected")

    def _fail(self, state):
        try:
            _require(self.state is not None and self.pid == os.getpid())
            if self.state not in ("failed", "memory_rejected"):
                self._publish(state)
        except Exception:
            raise LoadingProtocolError() from None
        finally:
            if self.gate is not None:
                try:
                    self.gate.close()
                except Exception:
                    raise LoadingProtocolError() from None

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is not None or self.state != "ready":
                self.fail()
        finally:
            if self.gate is not None:
                try:
                    self.gate.close()
                except Exception:
                    raise LoadingProtocolError() from None
        return False


def child_loading_session_from_environment(environ, *, expected_binding_digest, cancelled=None):
    try:
        _require(isinstance(environ, MutableMapping))
        fields = {
            key: environ.pop(key)
            for key in tuple(environ)
            if isinstance(key, str) and key.startswith(LOADING_ENV_PREFIX)
        }
        if not fields:
            return None
        _require(set(fields) == set(LOADING_ENV_KEYS))
        binding = LoadingBinding(*(fields[key] for key in LOADING_ENV_KEYS))
        _require(_digest(expected_binding_digest) and binding.binding_digest == expected_binding_digest)
        _require(cancelled is None or callable(cancelled))
        _validate_binding(binding)
        return LoadingSession(binding, cancelled)
    except Exception:
        raise LoadingProtocolError() from None
