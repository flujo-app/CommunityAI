"""Launch one bounded complete public-alpha CUDA route.

The image fixes the candidate manifest and public port. Runtime input is limited to
one authenticated discovery peer and the VM's observed public IPv4 address. The
entrypoint never enables training RPCs and always emits the bounded aggregate health
file consumed by the Gate 11 lifecycle controller.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

import drift
from drift.model_manifest import ModelManifest

_DEFAULT_MANIFEST_PATH = "/workspace/public-route/model-manifest.json"
_DEFAULT_CACHE_DIR = "/cache/model"
_IDENTITY_PATH = "/run/communityai/identity.key"
_HEALTH_STATE_PATH = "/run/communityai/health.json"
_PEER_ID_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,128}$")
_CANDIDATES: Mapping[str, Mapping[str, object]] = {
    "qwen3.5-2b": {
        "manifest_digest": "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "repository": "Qwen/Qwen3.5-2B",
        "port": 31337,
        "max_device_memory": "7GiB",
    },
    "gemma-4-e2b": {
        "manifest_digest": "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "repository": "google/gemma-4-E2B-it",
        "port": 31338,
        "max_device_memory": "15GiB",
    },
}


class PublicRouteConfigurationError(ValueError):
    """The immutable public-route image or bounded runtime input is invalid."""


def _bounded_env(name: str, *, maximum: int) -> str:
    value = os.environ.get(name)
    if value is None or not value or len(value) > maximum or any(character in value for character in "\x00\r\n"):
        raise PublicRouteConfigurationError(f"{name} is required and must be a bounded single-line value")
    return value


def _candidate() -> tuple[str, Mapping[str, object]]:
    candidate = _bounded_env("COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE", maximum=32)
    profile = _CANDIDATES.get(candidate)
    if profile is None:
        raise PublicRouteConfigurationError("public-route candidate is not in the immutable alpha set")
    return candidate, profile


def _public_ipv4() -> str:
    raw = _bounded_env("COMMUNITYAI_PUBLIC_ROUTE_IPV4", maximum=15)
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        raise PublicRouteConfigurationError("public-route address is not valid IPv4") from None
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or not address.is_global
        or address.is_multicast
        or address.is_unspecified
    ):
        raise PublicRouteConfigurationError("public-route address must be a global unicast IPv4 address")
    return address.compressed


def _initial_peer() -> str:
    value = _bounded_env("COMMUNITYAI_PUBLIC_ROUTE_INITIAL_PEER", maximum=2048)
    if (
        any(character.isspace() for character in value)
        or value.count("/p2p/") != 1
        or not value.startswith(("/ip4/", "/ip6/", "/dns/", "/dns4/", "/dns6/"))
    ):
        raise PublicRouteConfigurationError("public-route bootstrap peer must be one authenticated multiaddr")
    peer_id = value.rsplit("/p2p/", 1)[1]
    if not _PEER_ID_RE.fullmatch(peer_id):
        raise PublicRouteConfigurationError("public-route bootstrap peer identity is invalid")
    return value


def _load_manifest(profile: Mapping[str, object]) -> tuple[ModelManifest, Path]:
    manifest_path = Path(os.environ.get("COMMUNITYAI_PUBLIC_ROUTE_MANIFEST", _DEFAULT_MANIFEST_PATH))
    if os.fspath(manifest_path) != _DEFAULT_MANIFEST_PATH:
        raise PublicRouteConfigurationError("public-route manifest path is fixed by the image")
    try:
        manifest = ModelManifest.load(manifest_path)
        manifest.validate_runtime(drift.__version__)
    except Exception:
        raise PublicRouteConfigurationError("public-route manifest is invalid or incompatible") from None
    if manifest.digest_id != profile["manifest_digest"] or manifest.source.repository != profile["repository"]:
        raise PublicRouteConfigurationError("public-route manifest does not match the immutable candidate")
    return manifest, manifest_path


def build_worker_args() -> list[str]:
    _candidate_name, profile = _candidate()
    manifest, manifest_path = _load_manifest(profile)
    public_ip = _public_ipv4()
    initial_peer = _initial_peer()
    port = int(profile["port"])
    return [
        "drift",
        "server",
        manifest.source.repository,
        "--model_manifest",
        os.fspath(manifest_path),
        "--identity_path",
        _IDENTITY_PATH,
        "--initial_peers",
        initial_peer,
        "--block_indices",
        f"0:{manifest.model.num_blocks}",
        "--host_maddrs",
        f"/ip4/0.0.0.0/tcp/{port}",
        "--announce_maddrs",
        f"/ip4/{public_ip}/tcp/{port}",
        "--device",
        "cuda",
        "--max_device_memory",
        str(profile["max_device_memory"]),
        "--torch_dtype",
        manifest.runtime.dtype,
        "--attn_implementation",
        manifest.runtime.attention_implementation,
        "--quant_type",
        manifest.runtime.quantization,
        "--cache_dir",
        _DEFAULT_CACHE_DIR,
        "--health_state_path",
        _HEALTH_STATE_PATH,
        "--attn_cache_tokens",
        "512",
        "--max_batch_size",
        "1",
        "--max_chunk_size_bytes",
        str(16 * 1024 * 1024),
        "--throughput",
        "1.0",
        "--num_handlers",
        "1",
        "--admission_max_active_sessions",
        "8",
        "--admission_max_active_sessions_per_peer",
        "1",
        "--admission_global_session_rate",
        "2.0",
        "--admission_global_session_burst",
        "4",
        "--admission_peer_session_rate",
        "0.25",
        "--admission_peer_session_burst",
        "1",
        "--admission_max_tracked_peers",
        "512",
        "--admission_tracked_peer_ttl",
        "300",
        "--admission_max_pending_pushes",
        "4",
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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        raise PublicRouteConfigurationError("unexpected public-route node arguments")
    command = build_worker_args()
    os.execvp(command[0], command)
    raise AssertionError("os.execvp returned unexpectedly")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicRouteConfigurationError as exc:
        print(f"public-route node configuration failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
