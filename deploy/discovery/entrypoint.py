"""Prepare a Fly identity volume, drop root, and start the discovery-only DHT."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Mapping, Sequence

DISCOVERY_UID = 65532
DISCOVERY_GID = 65532
MAX_IDENTITY_KEY_BYTES = 16_384
IDENTITY_DIRECTORY = Path("/data")
IDENTITY_PATH = "/data/identity.key"
READINESS_PATH = "/run/communityai/readiness.json"
BOOTSTRAP_PATH = "/opt/communityai/bootstrap_node.py"
HOST_MADDR = "/ip4/0.0.0.0/tcp/31337"
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_INITIAL_PEER_RE = re.compile(r"^/ip4/[^/]+/tcp/31337/p2p/[^/]{20,128}$")
_ANNOUNCE_RE = re.compile(r"^/dns4/communityai-[a-z0-9](?:[a-z0-9-]{0,49}[a-z0-9])?\.fly\.dev/tcp/31337$")


class DiscoveryEntrypointError(RuntimeError):
    """The discovery seed cannot safely initialize its persistent identity."""


def _required_environment(environment: Mapping[str, str], name: str, pattern: re.Pattern[str]) -> str:
    value = environment.get(name)
    if value is None or len(value) > 2048 or pattern.fullmatch(value) is None:
        raise DiscoveryEntrypointError(f"{name} is missing or invalid")
    return value


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    if checker is None:
        return False
    try:
        return bool(checker(path))
    except OSError:
        raise DiscoveryEntrypointError("identity volume metadata could not be inspected") from None


def _validate_identity_metadata(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DiscoveryEntrypointError("identity key must be an unlinked regular file")
    if metadata.st_nlink != 1:
        raise DiscoveryEntrypointError("identity key must have exactly one link")
    if metadata.st_uid != DISCOVERY_UID or metadata.st_gid != DISCOVERY_GID:
        raise DiscoveryEntrypointError("identity key has unsafe ownership")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DiscoveryEntrypointError("identity key has unsafe permissions")
    if not 0 < metadata.st_size <= MAX_IDENTITY_KEY_BYTES:
        raise DiscoveryEntrypointError("identity key has an unsafe size")


def _validate_existing_identity(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise DiscoveryEntrypointError("identity key metadata could not be inspected") from None
    if _is_junction(path):
        raise DiscoveryEntrypointError("identity key must be an unlinked regular file")
    _validate_identity_metadata(metadata)


def _prepare_identity_directory(path: Path = IDENTITY_DIRECTORY) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise DiscoveryEntrypointError("identity volume is missing") from None
    if stat.S_ISLNK(metadata.st_mode) or _is_junction(path) or not stat.S_ISDIR(metadata.st_mode):
        raise DiscoveryEntrypointError("identity volume must be an unlinked directory")
    _validate_existing_identity(path / "identity.key")
    try:
        os.chown(path, DISCOVERY_UID, DISCOVERY_GID)
        os.chmod(path, 0o700)
    except OSError:
        raise DiscoveryEntrypointError("identity volume ownership could not be prepared") from None


def _drop_privileges() -> None:
    getuid = getattr(os, "geteuid", None)
    getgid = getattr(os, "getegid", None)
    getgroups = getattr(os, "getgroups", None)
    if getuid is None or getgid is None or getgroups is None or getuid() != 0:
        raise DiscoveryEntrypointError("discovery entrypoint must begin as root")
    try:
        os.setgroups([])
        os.setgid(DISCOVERY_GID)
        os.setuid(DISCOVERY_UID)
    except OSError:
        raise DiscoveryEntrypointError("discovery entrypoint could not drop root") from None
    if getuid() != DISCOVERY_UID or getgid() != DISCOVERY_GID or list(getgroups()) != []:
        raise DiscoveryEntrypointError("discovery entrypoint remained privileged")


def build_bootstrap_argv(environment: Mapping[str, str]) -> list[str]:
    source_commit = _required_environment(environment, "COMMUNITYAI_SOURCE_COMMIT", _SOURCE_COMMIT_RE)
    initial_peer = _required_environment(environment, "COMMUNITYAI_DISCOVERY_INITIAL_PEER", _INITIAL_PEER_RE)
    announce_maddr = _required_environment(environment, "COMMUNITYAI_DISCOVERY_ANNOUNCE_MADDR", _ANNOUNCE_RE)
    return [
        sys.executable,
        "-u",
        BOOTSTRAP_PATH,
        "--identity-path",
        IDENTITY_PATH,
        "--host-maddr",
        HOST_MADDR,
        "--announce-maddr",
        announce_maddr,
        "--initial-peer",
        initial_peer,
        "--strict-first-start",
        "--source-commit",
        source_commit,
        "--readiness-path",
        READINESS_PATH,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise DiscoveryEntrypointError("discovery entrypoint accepts no command-line overrides")
    command = build_bootstrap_argv(os.environ)
    os.umask(0o077)
    _prepare_identity_directory()
    _drop_privileges()
    os.execv(command[0], command)
    raise DiscoveryEntrypointError("discovery bootstrap exec unexpectedly returned")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except DiscoveryEntrypointError as exc:
        print(f"discovery seed initialization failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
