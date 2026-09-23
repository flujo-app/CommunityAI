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


def _validate_parent(parent: Path) -> Path:
    try:
        if any(_is_link_or_junction(candidate) for candidate in (parent, *parent.parents)):
            raise NodeConfigWriteLockError("node config writer refuses a linked lock directory")
        return parent.resolve(strict=True)
    except OSError as exc:
        raise NodeConfigWriteLockError("node config lock directory could not be verified") from exc


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


def _require_bound_lock(info, expected_identity):
    if expected_identity is not None and (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or (info.st_dev, info.st_ino) != expected_identity
    ):
        raise NodeConfigWriteLockError("bound writer lock identity was lost or replaced")


@contextmanager
def node_config_write_lock(config_path: Path | str, *, expected_identity=None) -> Iterator[None]:
    """Every anchored config writer inherits the exact admitted lock identity."""
    bound = require_node_config_write_authority(config_path)
    if bound is not None:
        if expected_identity is not None and expected_identity != bound:
            raise NodeConfigWriteLockError("configuration writer supplied a different bound identity")
        expected_identity = bound
    with persistent_sidecar_lock(config_path, expected_identity=expected_identity):
        if require_node_config_write_authority(config_path) != bound:
            raise NodeConfigWriteLockError("configuration write authority changed while acquiring its lock")
        yield
        if require_node_config_write_authority(config_path) != bound:
            raise NodeConfigWriteLockError("configuration write authority changed during its transaction")


def require_node_config_write_authority(config_path):
    from communityai_anchor.linux_anchor_entry import anchored_catalog_paths, require_admitted_catalog_writer

    path = Path(os.path.abspath(os.path.expanduser(os.fspath(config_path))))
    try:
        if anchored_catalog_paths(path.parent, path):
            return require_admitted_catalog_writer(path.parent, path)["node/.node-config.json.write.lock"]
    except Exception:
        raise NodeConfigWriteLockError("Anchored configuration writes require intact admitted authority") from None
    return None


@contextmanager
def persistent_sidecar_lock(config_path: Path | str, *, expected_identity=None) -> Iterator[None]:
    """Non-blocking kernel primitive, also used for separate compute budgets.

    This lock is intentionally a persistent sidecar. Removing it after unlock could
    let two processes lock different inodes. Configuration writers must use the
    authority-checking node_config_write_lock wrapper, not this bare primitive.
    """

    absolute_config = Path(os.path.abspath(os.path.expanduser(os.fspath(config_path))))
    canonical_parent = _validate_parent(absolute_config.parent)
    if _is_link_or_junction(absolute_config):
        raise NodeConfigWriteLockError("node config writer refuses a linked config path")
    try:
        canonical_config = (
            absolute_config.resolve(strict=True)
            if absolute_config.exists()
            else canonical_parent / absolute_config.name
        )
    except OSError as exc:
        raise NodeConfigWriteLockError("node config path could not be verified") from exc
    lock_path = node_config_lock_path(canonical_config)
    if _is_link_or_junction(lock_path):
        raise NodeConfigWriteLockError("node config writer refuses a linked lock file")

    flags = os.O_RDWR | (os.O_CREAT if expected_identity is None else 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = None
    acquired = False
    try:
        if expected_identity is not None:
            _require_bound_lock(lock_path.lstat(), expected_identity)
        descriptor = os.open(lock_path, flags, 0o600)
        opened = os.fstat(descriptor)
        _require_bound_lock(opened, expected_identity)
        if not stat.S_ISREG(opened.st_mode):
            raise NodeConfigWriteLockError("node config lock must be a regular file")
        try:
            path_stat = lock_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise NodeConfigWriteLockError("node config lock could not be verified") from exc
        if not os.path.samestat(opened, path_stat):
            raise NodeConfigWriteLockError("node config lock changed while it was opened")
        _require_bound_lock(path_stat, expected_identity)
        if expected_identity is None:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
        try:
            _acquire(descriptor)
        except OSError as exc:
            raise NodeConfigWriteLockError("another node config writer is active") from exc
        acquired = True
        current = lock_path.lstat()
        _require_bound_lock(current, expected_identity)
        if not os.path.samestat(opened, current):
            raise NodeConfigWriteLockError("node config lock changed while it was acquired")
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
