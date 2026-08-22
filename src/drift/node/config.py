"""Strict, secret-free configuration for a multi-model local node."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

NODE_CONFIG_SCHEMA_VERSION = 1


class NodeConfigError(ValueError):
    """The node configuration is malformed or cannot be read."""


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise NodeConfigError(f"{field} must be a JSON object")
    return value


def _require_fields(
    value: Mapping[str, Any], field: str, *, required: Tuple[str, ...], optional: Tuple[str, ...] = ()
) -> None:
    actual = set(value)
    missing = set(required) - actual
    extra = actual - set(required) - set(optional)
    if missing:
        raise NodeConfigError(f"{field} is missing required field(s): {', '.join(sorted(missing))}")
    if extra:
        raise NodeConfigError(f"{field} has unknown field(s): {', '.join(sorted(extra))}")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NodeConfigError(f"{field} must be a non-empty string")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NodeConfigError(f"{field} must be an integer >= 1")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise NodeConfigError(f"{field} must be a boolean")
    return value


def _require_positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NodeConfigError(f"{field} must be a finite number > 0")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise NodeConfigError(f"{field} must be a finite number > 0")
    return result


def _require_string_list(value: Any, field: str, *, nonempty: bool = False) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise NodeConfigError(f"{field} must be a JSON array")
    result = tuple(_require_string(item, f"{field}[]") for item in value)
    if nonempty and not result:
        raise NodeConfigError(f"{field} must contain at least one value")
    if len(set(result)) != len(result):
        raise NodeConfigError(f"{field} must not contain duplicates")
    return result


def _resolve_path(value: Any, field: str, base_dir: Path) -> Path:
    raw_path = Path(_require_string(value, field)).expanduser()
    if not raw_path.is_absolute():
        raw_path = base_dir / raw_path
    return raw_path.resolve()


@dataclass(frozen=True)
class NodeModelConfig:
    """Startup inputs for one exact manifested swarm."""

    manifest_path: Path
    initial_peers: Tuple[str, ...]
    cache_dir: Optional[Path] = None
    revocation_files: Tuple[Path, ...] = ()
    request_timeout: float = 30.0
    max_retries: int = 3

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], *, base_dir: Path, index: int) -> "NodeModelConfig":
        field = f"models[{index}]"
        source = _require_object(source, field)
        _require_fields(
            source,
            field,
            required=("manifest", "initial_peers"),
            optional=("cache_dir", "revocation_files", "request_timeout", "max_retries"),
        )
        cache_value = source.get("cache_dir")
        cache_dir = None if cache_value is None else _resolve_path(cache_value, f"{field}.cache_dir", base_dir)
        revocation_values = _require_string_list(source.get("revocation_files", []), f"{field}.revocation_files")
        return cls(
            manifest_path=_resolve_path(source["manifest"], f"{field}.manifest", base_dir),
            initial_peers=_require_string_list(source["initial_peers"], f"{field}.initial_peers", nonempty=True),
            cache_dir=cache_dir,
            revocation_files=tuple(
                _resolve_path(value, f"{field}.revocation_files[]", base_dir) for value in revocation_values
            ),
            request_timeout=_require_positive_number(source.get("request_timeout", 30), f"{field}.request_timeout"),
            max_retries=_require_positive_int(source.get("max_retries", 3), f"{field}.max_retries"),
        )


@dataclass(frozen=True)
class WorkerConfig:
    """One isolated contribution worker controlled by the local node."""

    worker_id: str
    model: str
    identity_path: Path
    num_blocks: Optional[int] = None
    block_indices: Optional[str] = None
    enabled: bool = False
    auto_restart: bool = True
    restart_backoff: float = 5.0
    device: Optional[str] = None
    cache_dir: Optional[Path] = None
    max_disk_space: Optional[str] = None
    throughput: float | str = "auto"
    port: Optional[int] = None
    public_ip: Optional[str] = None

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], *, base_dir: Path, index: int) -> "WorkerConfig":
        field = f"workers[{index}]"
        source = _require_object(source, field)
        _require_fields(
            source,
            field,
            required=("id", "model", "identity_path"),
            optional=(
                "num_blocks",
                "block_indices",
                "enabled",
                "auto_restart",
                "restart_backoff",
                "device",
                "cache_dir",
                "max_disk_space",
                "throughput",
                "port",
                "public_ip",
            ),
        )
        worker_id = _require_string(source["id"], f"{field}.id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", worker_id):
            raise NodeConfigError(f"{field}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")

        num_blocks_value = source.get("num_blocks")
        block_indices_value = source.get("block_indices")
        if (num_blocks_value is None) == (block_indices_value is None):
            raise NodeConfigError(f"{field} must provide exactly one of num_blocks or block_indices")
        num_blocks = (
            None if num_blocks_value is None else _require_positive_int(num_blocks_value, f"{field}.num_blocks")
        )
        block_indices = None
        if block_indices_value is not None:
            block_indices = _require_string(block_indices_value, f"{field}.block_indices")
            match = re.fullmatch(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)", block_indices)
            if match is None or int(match.group(1)) >= int(match.group(2)):
                raise NodeConfigError(f"{field}.block_indices must be a non-empty start:end range")

        throughput_value = source.get("throughput", "auto")
        if isinstance(throughput_value, str):
            if throughput_value != "auto":
                raise NodeConfigError(f"{field}.throughput must be 'auto' or a finite number > 0")
            throughput: float | str = throughput_value
        else:
            throughput = _require_positive_number(throughput_value, f"{field}.throughput")

        port_value = source.get("port")
        port = None if port_value is None else _require_positive_int(port_value, f"{field}.port")
        if port is not None and port > 65535:
            raise NodeConfigError(f"{field}.port must be <= 65535")

        def optional_string(name: str) -> Optional[str]:
            value = source.get(name)
            return None if value is None else _require_string(value, f"{field}.{name}")

        cache_value = source.get("cache_dir")
        return cls(
            worker_id=worker_id,
            model=_require_string(source["model"], f"{field}.model"),
            identity_path=_resolve_path(source["identity_path"], f"{field}.identity_path", base_dir),
            num_blocks=num_blocks,
            block_indices=block_indices,
            enabled=_require_bool(source.get("enabled", False), f"{field}.enabled"),
            auto_restart=_require_bool(source.get("auto_restart", True), f"{field}.auto_restart"),
            restart_backoff=_require_positive_number(source.get("restart_backoff", 5), f"{field}.restart_backoff"),
            device=optional_string("device"),
            cache_dir=None if cache_value is None else _resolve_path(cache_value, f"{field}.cache_dir", base_dir),
            max_disk_space=optional_string("max_disk_space"),
            throughput=throughput,
            port=port,
            public_ip=optional_string("public_ip"),
        )


@dataclass(frozen=True)
class NodeConfig:
    """Versioned multi-model configuration loaded atomically at node startup."""

    schema_version: int
    max_loaded_models: int
    models: Tuple[NodeModelConfig, ...]
    workers: Tuple[WorkerConfig, ...] = ()
    discovery_update_period: float = 30.0
    discovery_startup_timeout: float = 15.0

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], *, base_dir: Path) -> "NodeConfig":
        source = _require_object(source, "node config")
        _require_fields(
            source,
            "node config",
            required=("schema_version", "models"),
            optional=("max_loaded_models", "discovery_update_period", "discovery_startup_timeout", "workers"),
        )
        schema_version = _require_positive_int(source["schema_version"], "schema_version")
        if schema_version != NODE_CONFIG_SCHEMA_VERSION:
            raise NodeConfigError(f"Unsupported schema_version {schema_version}; expected {NODE_CONFIG_SCHEMA_VERSION}")
        models_value = source["models"]
        if not isinstance(models_value, list) or not models_value:
            raise NodeConfigError("models must be a non-empty JSON array")
        models = tuple(
            NodeModelConfig.from_dict(item, base_dir=base_dir, index=index) for index, item in enumerate(models_value)
        )
        manifest_paths = [model.manifest_path for model in models]
        if len(set(manifest_paths)) != len(manifest_paths):
            raise NodeConfigError("models must not configure the same manifest path more than once")
        workers_value = source.get("workers", [])
        if not isinstance(workers_value, list):
            raise NodeConfigError("workers must be a JSON array")
        workers = tuple(
            WorkerConfig.from_dict(item, base_dir=base_dir, index=index) for index, item in enumerate(workers_value)
        )
        worker_ids = [worker.worker_id.casefold() for worker in workers]
        if len(set(worker_ids)) != len(worker_ids):
            raise NodeConfigError("worker ids must be unique case-insensitively")
        return cls(
            schema_version=schema_version,
            max_loaded_models=_require_positive_int(source.get("max_loaded_models", 1), "max_loaded_models"),
            models=models,
            workers=workers,
            discovery_update_period=_require_positive_number(
                source.get("discovery_update_period", 30), "discovery_update_period"
            ),
            discovery_startup_timeout=_require_positive_number(
                source.get("discovery_startup_timeout", 15), "discovery_startup_timeout"
            ),
        )

    @classmethod
    def from_json(cls, source: str, *, base_dir: Path) -> "NodeConfig":
        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise NodeConfigError(f"Node config JSON contains duplicate object key {key!r}")
                result[key] = value
            return result

        def reject_non_finite(value):
            raise NodeConfigError(f"Node config JSON contains non-finite number {value}")

        try:
            value = json.loads(source, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
        except json.JSONDecodeError as exc:
            raise NodeConfigError(f"Invalid node config JSON: {exc}") from exc
        return cls.from_dict(value, base_dir=base_dir)

    @classmethod
    def load(cls, path: Path | str) -> "NodeConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            source = config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise NodeConfigError(f"Could not read node config {config_path}: {exc}") from exc
        return cls.from_json(source, base_dir=config_path.parent)
