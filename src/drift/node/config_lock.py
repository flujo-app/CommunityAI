"""Cross-process serialization for writers of one node configuration."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class NodeConfigWriteLockError(RuntimeError):
    """A node-config writer could not acquire the shared transaction lock."""


def node_config_lock_path(config_path: Path | str) -> Path:
    """Return the stable sidecar lock shared by every node-config writer."""
    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(config_path))))
    return absolute.with_name(f".{absolute.name}.write.lock")


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda candidate: False)(path))


def _validate_parent(parent: Path) -> None:
    try:
        if _is_link_or_junction(parent):
            raise NodeConfigWriteLockError("node config writer refuses a linked lock directory")
        resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise NodeConfigWriteLockError("node config lock directory could not be verified") from exc
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(parent)):
        raise NodeConfigWriteLockError("node config writer refuses a linked lock directory")


def _acquire(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size < 1:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def node_config_write_lock(config_path: Path | str) -> Iterator[None]:
    """Hold the non-blocking cross-process lock for one config transaction.

    This lock is intentionally a persistent sidecar. Removing it after unlock could
    let two processes lock different inodes. All repository-owned node-config
    writers use this protocol before checking or replacing the config.
    """

    lock_path = node_config_lock_path(config_path)
    _validate_parent(lock_path.parent)
    if _is_link_or_junction(lock_path):
        raise NodeConfigWriteLockError("node config writer refuses a linked lock file")

    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    acquired = False
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise NodeConfigWriteLockError("node config lock must be a regular file")
        try:
            path_stat = lock_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise NodeConfigWriteLockError("node config lock could not be verified") from exc
        if not os.path.samestat(opened, path_stat):
            raise NodeConfigWriteLockError("node config lock changed while it was opened")
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        try:
            _acquire(descriptor)
        except OSError as exc:
            raise NodeConfigWriteLockError("another node config writer is active") from exc
        acquired = True
        yield
    except NodeConfigWriteLockError:
        raise
    except OSError as exc:
        raise NodeConfigWriteLockError("node config write lock could not be acquired") from exc
    finally:
        if descriptor is not None:
            if acquired:
                try:
                    _release(descriptor)
                except OSError:
                    pass
            os.close(descriptor)
