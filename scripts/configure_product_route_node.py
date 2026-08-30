"""Configure one signed-catalog node as a bounded public-alpha seed.

The catalog bootstrap deliberately creates a conservative one-block worker with
sharing disabled.  Provider-owned seed nodes use this helper to apply a local
resource policy and a full-route worker span without changing the signed catalog,
the manifested model identity, or the normal automatic-placement code path.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from drift.model_manifest import ModelManifest
from drift.node.config import NodeConfig, NodeConfigError

SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024


class ProductRouteConfigError(ValueError):
    """A bootstrapped node cannot be safely configured as a seed route."""


@dataclass(frozen=True)
class SeedProfile:
    role: str
    manifest_digest: str
    num_blocks: int
    public_port: int
    max_disk_space: str
    max_vram: str


PROFILES = {
    "primary": SeedProfile(
        role="primary",
        manifest_digest="sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        num_blocks=24,
        public_port=31337,
        max_disk_space="32GiB",
        max_vram="7GiB",
    ),
    "standby": SeedProfile(
        role="standby",
        manifest_digest="sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        num_blocks=35,
        public_port=31338,
        max_disk_space="32GiB",
        max_vram="15GiB",
    ),
}


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductRouteConfigError(f"node configuration repeats field {key!r}")
        result[key] = value
    return result


def _load_config(path: Path) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ProductRouteConfigError("node configuration must be a regular non-symlink file")
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ProductRouteConfigError("node configuration exceeds the bounded size")
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_reject_duplicate_keys)
    except ProductRouteConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductRouteConfigError("node configuration is not readable strict JSON") from exc
    if not isinstance(value, dict):
        raise ProductRouteConfigError("node configuration must be a JSON object")
    return value


def _validate_bootstrap_shape(source: Mapping[str, Any], profile: SeedProfile, *, config_path: Path) -> None:
    if source.get("schema_version") != SCHEMA_VERSION:
        raise ProductRouteConfigError("node configuration schema is unsupported")
    workers = source.get("workers")
    if not isinstance(workers, list) or len(workers) != 1 or not isinstance(workers[0], dict):
        raise ProductRouteConfigError("signed bootstrap must create exactly one automatic worker")
    worker = workers[0]
    if worker.get("id") != "automatic" or worker.get("model") != "auto":
        raise ProductRouteConfigError("signed bootstrap automatic worker identity is invalid")

    models = source.get("models")
    if not isinstance(models, list) or len(models) != 2:
        raise ProductRouteConfigError("public-alpha bootstrap must contain exactly two manifested models")
    digests = set()
    for index, model in enumerate(models):
        if not isinstance(model, dict) or not isinstance(model.get("manifest"), str):
            raise ProductRouteConfigError(f"model {index} does not contain a manifest path")
        manifest_path = Path(model["manifest"])
        if not manifest_path.is_absolute():
            manifest_path = config_path.parent / manifest_path
        try:
            manifest = ModelManifest.load(manifest_path)
        except Exception as exc:
            raise ProductRouteConfigError(f"model {index} manifest is invalid") from exc
        digests.add(manifest.digest_id)
    expected = {item.manifest_digest for item in PROFILES.values()}
    if digests != expected or profile.manifest_digest not in digests:
        raise ProductRouteConfigError("node configuration does not contain the exact signed alpha candidates")


def _atomic_replace(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def configure_product_route_node(
    config_path: Path,
    *,
    role: str,
    public_ip: str,
    cache_root: Path,
) -> Mapping[str, Any]:
    path = config_path.expanduser().resolve()
    profile = PROFILES.get(role)
    if profile is None:
        raise ProductRouteConfigError("route role must be primary or standby")
    try:
        normalized_ip = str(ipaddress.IPv4Address(public_ip))
    except ipaddress.AddressValueError as exc:
        raise ProductRouteConfigError("public route address must be one canonical IPv4 address") from exc
    root = cache_root.expanduser().resolve()
    source = _load_config(path)
    _validate_bootstrap_shape(source, profile, config_path=path)

    worker = source["workers"][0]
    worker.update(
        {
            "num_blocks": profile.num_blocks,
            "enabled": True,
            "auto_restart": True,
            "restart_backoff": 5,
            "device": "cuda:0",
            "cache_dir": str(root / profile.role),
            "max_disk_space": profile.max_disk_space,
            "max_vram": profile.max_vram,
            "port": profile.public_port,
            "public_ip": normalized_ip,
        }
    )
    source["contribution_policy"] = {
        "sharing_enabled": True,
        "allowed_models": [profile.manifest_digest],
        "preferred_models": [profile.manifest_digest],
        "denied_models": [],
        "max_disk_space": profile.max_disk_space,
        "max_vram": profile.max_vram,
        "pause_timeout": 10,
    }

    try:
        NodeConfig.from_dict(source, base_dir=path.parent)
    except NodeConfigError as exc:
        raise ProductRouteConfigError(f"configured product node is invalid: {exc}") from exc
    payload = (json.dumps(source, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_CONFIG_BYTES:
        raise ProductRouteConfigError("configured product node exceeds the bounded size")
    root.mkdir(parents=True, exist_ok=True)
    _atomic_replace(path, payload)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "communityai-product-route-node-configuration",
        "result": "configured",
        "role": profile.role,
        "manifest_digest": profile.manifest_digest,
        "num_blocks": profile.num_blocks,
        "public_port": profile.public_port,
        "automatic_placement": True,
        "sharing_enabled": True,
        "model_artifacts_embedded_in_runtime": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure one signed-catalog node as a bounded public route")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--role", choices=tuple(PROFILES), required=True)
    parser.add_argument("--public-ip", required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = configure_product_route_node(
            args.config,
            role=args.role,
            public_ip=args.public_ip,
            cache_root=args.cache_root,
        )
    except ProductRouteConfigError as exc:
        print(f"product route configuration failed: {exc}")
        return 1
    print(json.dumps(report, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
