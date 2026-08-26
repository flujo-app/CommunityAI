"""Run one role of an isolated Fly Machines qualification swarm.

The provider adapter provisions this entrypoint in an operator-supplied image that
already contains the exact candidate manifest and immutable model cache. Networking
uses only the Fly app's private IPv6 network. The identity command exposes the
public PeerID to the local adapter without exposing the private identity bytes.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Sequence

import drift
from drift.model_manifest import ModelManifest
from drift.protocol_identity import NodeIdentity

_IDENTITY_MARKER = "COMMUNITYAI_QUALIFICATION_IDENTITY="
_BLOCKS_RE = re.compile(r"^(0|[1-9][0-9]*):([1-9][0-9]*)$")
_DEFAULT_IDENTITY_PATH = "/tmp/communityai-qualification.id"
_DEFAULT_MANIFEST_PATH = "/workspace/model-manifest.json"
_DEFAULT_CACHE_DIR = "/cache"
_DEFAULT_PORT = 31337
_FLY_PRIVATE_NETWORK = ipaddress.ip_network("fdaa::/16")


class NodeConfigurationError(ValueError):
    """The qualification image or machine environment is invalid."""


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise NodeConfigurationError(f"{name} is required")
    return value


def _bounded_env(name: str, default: str, *, maximum: int = 4096) -> str:
    value = os.environ.get(name, default)
    if not value or "\x00" in value or len(value) > maximum:
        raise NodeConfigurationError(f"{name} is not a safe bounded value")
    return value


def _validated_private_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise NodeConfigurationError("Fly private address is not valid IPv6") from exc
    if not isinstance(address, ipaddress.IPv6Address) or address not in _FLY_PRIVATE_NETWORK:
        raise NodeConfigurationError("Fly private address is not a private 6PN IPv6 address")
    return address.compressed


def _private_ip() -> str:
    configured = os.environ.get("FLY_PRIVATE_IP")
    if configured:
        return _validated_private_ip(configured)
    hostname = socket.gethostname()
    for candidate in (hostname, f"{hostname}.vm.internal"):
        try:
            addresses = socket.getaddrinfo(candidate, None, socket.AF_INET6)
        except socket.gaierror:
            continue
        for address in addresses:
            try:
                return _validated_private_ip(address[4][0])
            except NodeConfigurationError:
                continue
    raise NodeConfigurationError("could not determine the Fly private 6PN IPv6 address")


def _identity_path() -> Path:
    return Path(_bounded_env("COMMUNITYAI_QUALIFICATION_IDENTITY_PATH", _DEFAULT_IDENTITY_PATH))


def _manifest_path() -> Path:
    return Path(_bounded_env("COMMUNITYAI_QUALIFICATION_MANIFEST", _DEFAULT_MANIFEST_PATH))


def _port() -> int:
    raw = _bounded_env("COMMUNITYAI_QUALIFICATION_PORT", str(_DEFAULT_PORT), maximum=5)
    try:
        value = int(raw)
    except ValueError as exc:
        raise NodeConfigurationError("COMMUNITYAI_QUALIFICATION_PORT must be an integer") from exc
    if not 1 <= value <= 65535:
        raise NodeConfigurationError("COMMUNITYAI_QUALIFICATION_PORT is outside 1-65535")
    return value


def _block_indices(manifest: ModelManifest) -> str:
    value = _required_env("COMMUNITYAI_QUALIFICATION_BLOCKS")
    match = _BLOCKS_RE.fullmatch(value)
    if match is None:
        raise NodeConfigurationError("COMMUNITYAI_QUALIFICATION_BLOCKS must be one start:end span")
    start, end = (int(part) for part in match.groups())
    if start < 0 or end <= start or end > manifest.model.num_blocks:
        raise NodeConfigurationError("worker block span is outside the manifested block range")
    if start == 0 and end == manifest.model.num_blocks:
        raise NodeConfigurationError("qualification workers must not serve the full manifested range")
    return value


def build_bootstrap_args() -> list[str]:
    ip = _private_ip()
    port = _port()
    return [
        "drift",
        "dht",
        "--host_maddrs",
        f"/ip6/::/tcp/{port}",
        "--announce_maddrs",
        f"/ip6/{ip}/tcp/{port}",
        "--identity_path",
        str(_identity_path()),
        "--no_relay",
        "--refresh_period",
        "5",
    ]


def build_worker_args() -> list[str]:
    manifest_path = _manifest_path()
    manifest = ModelManifest.load(manifest_path)
    manifest.validate_runtime(drift.__version__)
    ip = _private_ip()
    port = _port()
    initial_peer = _required_env("COMMUNITYAI_QUALIFICATION_INITIAL_PEER")
    if len(initial_peer) > 2048 or "/p2p/" not in initial_peer:
        raise NodeConfigurationError("qualification bootstrap peer must be a bounded authenticated multiaddr")
    cache_dir = _bounded_env("COMMUNITYAI_QUALIFICATION_CACHE_DIR", _DEFAULT_CACHE_DIR)
    args = [
        "drift",
        "server",
        manifest.source.repository,
        "--model_manifest",
        str(manifest_path),
        "--identity_path",
        str(_identity_path()),
        "--initial_peers",
        initial_peer,
        "--block_indices",
        _block_indices(manifest),
        "--host_maddrs",
        f"/ip6/::/tcp/{port}",
        "--announce_maddrs",
        f"/ip6/{ip}/tcp/{port}",
        "--device",
        _bounded_env("COMMUNITYAI_QUALIFICATION_DEVICE", "cpu", maximum=32),
        "--torch_dtype",
        manifest.runtime.dtype,
        "--attn_implementation",
        manifest.runtime.attention_implementation,
        "--quant_type",
        manifest.runtime.quantization,
        "--cache_dir",
        cache_dir,
        "--attn_cache_tokens",
        _bounded_env("COMMUNITYAI_QUALIFICATION_ATTN_CACHE_TOKENS", "1024", maximum=16),
        "--max_batch_size",
        _bounded_env("COMMUNITYAI_QUALIFICATION_MAX_BATCH_SIZE", "64", maximum=16),
        "--max_chunk_size_bytes",
        str(16 * 1024 * 1024),
        "--throughput",
        _bounded_env("COMMUNITYAI_QUALIFICATION_THROUGHPUT", "1.0", maximum=32),
        "--update_period",
        "5",
        "--expiration",
        "30",
        "--request_timeout",
        "60",
        "--session_timeout",
        "60",
        "--step_timeout",
        "30",
        "--no_auto_relay",
    ]
    return args


def print_identity() -> None:
    identity = NodeIdentity.load(_identity_path())
    payload = {"schema_version": 1, "peer_id": str(identity.peer_id)}
    print(f"{_IDENTITY_MARKER}{json.dumps(payload, sort_keys=True)}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["identity"]:
        print_identity()
        return 0
    if arguments:
        raise NodeConfigurationError("unexpected qualification node arguments")
    role = _required_env("COMMUNITYAI_QUALIFICATION_ROLE")
    if role == "bootstrap":
        command = build_bootstrap_args()
    elif role == "worker":
        command = build_worker_args()
    else:
        raise NodeConfigurationError("COMMUNITYAI_QUALIFICATION_ROLE must be bootstrap or worker")
    os.execvp(command[0], command)
    raise AssertionError("os.execvp returned unexpectedly")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NodeConfigurationError as exc:
        print(f"qualification node configuration failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
