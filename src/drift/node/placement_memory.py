"""Manifest-bound, metadata-only device-memory profiles for automatic placement.

Only declared config/index artifacts may be materialized. CPU/CUDA memory estimates
use the manifest's explicit execution dtype and quantization, including BF16 expansion
of FP8 checkpoints. No accelerator tensors or native quantization probes are created.
The child still owns device availability, allocator enforcement and native capability
checks; an estimate is not permission or evidence that a backend can execute a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import torch

import drift
from drift.constants import DTYPE_MAP
from drift.model_manifest import ManifestArtifactVerifier, ManifestError, ModelManifest, select_manifest_block_artifacts
from drift.node.contribution_planner import MAX_AUTOMATIC_PLACEMENT_BLOCKS
from drift.server.memory_budget import ModelMemoryProfile, build_model_memory_profile
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.convert_block import QuantType
from drift.utils.hardware import normalize_device, supports_dtype

MAX_CONFIG_METADATA_BYTES = 1024**2
MAX_INDEX_METADATA_BYTES = 16 * 1024**2
MAX_MEMORY_PROFILE_CACHE_ENTRIES = 16


@dataclass(frozen=True)
class PlacementModelMetadata:
    """Immutable sizing inputs; ``device`` is informational, never a physical pin.

    No native capability or device availability is qualified by this record. The
    same declared CUDA geometry can be reused across separately verified cards.
    """

    manifest_digest: str
    cache_root: Path
    metadata_root: Path
    block_prefix: str
    weight_map: Optional[Mapping[str, str]]
    memory_profile: ModelMemoryProfile
    device: str
    native_quantization_probe_required: bool


class PlacementMemoryCache:
    """Bounded cache of small immutable profiles, never config objects or weight maps.

    Metadata is freshly verified by the loader even on a profile hit. Callers that
    retain complete metadata should separately bound its lifetime and memory usage.
    Device capability is deliberately neither asserted nor cached here.
    """

    def __init__(self, max_entries: int = MAX_MEMORY_PROFILE_CACHE_ENTRIES):
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_MEMORY_PROFILE_CACHE_ENTRIES:
            raise ValueError(f"profile cache capacity must be between 1 and {MAX_MEMORY_PROFILE_CACHE_ENTRIES}")
        self._max_entries = max_entries
        self._profiles: OrderedDict[tuple, ModelMemoryProfile] = OrderedDict()
        self._lock = threading.Lock()

    def _get(self, key: tuple) -> Optional[ModelMemoryProfile]:
        with self._lock:
            profile = self._profiles.get(key)
            if profile is not None:
                self._profiles.move_to_end(key)
            return profile

    def _put(self, key: tuple, profile: ModelMemoryProfile) -> None:
        with self._lock:
            self._profiles[key] = profile
            self._profiles.move_to_end(key)
            while len(self._profiles) > self._max_entries:
                self._profiles.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._profiles)


def _canonical_root(path) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(path)))))


def _verified_config_document(path: Path, artifact) -> dict:
    # Read once more after the verifier and bind the bytes used for the redirect
    # check, rather than trusting an earlier file-stat result.
    with path.open("rb") as stream:
        payload = stream.read(MAX_CONFIG_METADATA_BYTES + 1)
    if len(payload) != artifact.size or hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ManifestError("Placement config changed while metadata was being read")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ManifestError("Placement config contains duplicate JSON keys")
            result[key] = value
        return result

    def finite_json(value):
        raise ManifestError("Placement config contains non-finite JSON values")

    try:
        document = json.loads(payload, object_pairs_hook=unique_keys, parse_constant=finite_json)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError("Placement config is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ManifestError("Placement config must be a JSON object")
    if "configuration_files" in document:
        raise ManifestError("Placement config redirects require separately verified configuration support")
    return document


def load_placement_memory(
    manifest: ModelManifest,
    *,
    device: str | torch.device,
    cache_dir: Path | str,
    max_disk_space: int,
    token: Optional[str] = None,
    artifact_root: Optional[Path | str] = None,
    attn_cache_tokens: Optional[int] = None,
    profile_cache: Optional[PlacementMemoryCache] = None,
) -> PlacementModelMetadata:
    """Verify config/index metadata and build a single-device execution estimate.

    With ``artifact_root`` only that local directory is read; missing files fail
    without download. Otherwise the existing verifier may fetch missing metadata
    from the pinned source revision, under ``max_disk_space``. No weight/tokenizer
    artifact is opened or downloaded. A returned index has been validated for every
    model layer by the existing artifact selector and is immutable.

    INT8/NF4 retain their declared size estimates. The child must run its native
    quantization probe before loading weights; this helper does not qualify it.
    MPS/XPU require a separate exact runtime/device contract and are rejected here.
    """
    if not isinstance(manifest, ModelManifest):
        raise TypeError("placement memory requires a ModelManifest")
    manifest.validate_runtime(drift.__version__)
    if not 1 <= manifest.model.num_blocks <= MAX_AUTOMATIC_PLACEMENT_BLOCKS:
        raise ManifestError("Placement model exceeds the bounded automatic layer count")
    if manifest.runtime.adapter_profile != "none":
        raise ManifestError("Placement memory does not support executable adapter profiles")
    if type(max_disk_space) is not int or max_disk_space < 1:
        raise ValueError("max_disk_space must be a positive integer")
    if attn_cache_tokens is not None and (type(attn_cache_tokens) is not int or attn_cache_tokens < 1):
        raise ValueError("attn_cache_tokens must be a positive integer or None")
    if profile_cache is not None and not isinstance(profile_cache, PlacementMemoryCache):
        raise TypeError("profile_cache must be a PlacementMemoryCache")
    selected_device = normalize_device(torch.device(device))
    if selected_device.type not in ("cpu", "cuda"):
        raise ManifestError("Placement memory currently supports explicit CPU or CUDA devices only")
    dtype = DTYPE_MAP[manifest.runtime.dtype]
    quant_type = QuantType[manifest.runtime.quantization.upper()]
    dtype_error = supports_dtype(selected_device, dtype)
    if dtype_error is not None:
        raise ManifestError(dtype_error)

    configs = manifest.artifacts_for_roles({"config"})
    indices = manifest.artifacts_for_roles({"weight_index"})
    if len(configs) != 1 or configs[0].path != "config.json":
        raise ManifestError("Placement requires exactly one declared config.json")
    if len(indices) > 1:
        raise ManifestError("Placement supports at most one declared checkpoint index")
    if configs[0].size > MAX_CONFIG_METADATA_BYTES or any(a.size > MAX_INDEX_METADATA_BYTES for a in indices):
        raise ManifestError("Placement metadata exceeds its bounded planning size")
    metadata = configs + indices
    if sum(artifact.size for artifact in metadata) > max_disk_space:
        raise ManifestError("Placement metadata exceeds the configured disk budget")
    cache_root = _canonical_root(cache_dir)
    local_root = None if artifact_root is None else _canonical_root(artifact_root)
    verifier = ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        token=token if manifest.model.gated else False,
        cache_dir=cache_root,
        max_disk_space=max_disk_space,
        artifact_root=local_root,
        allowed_paths=tuple(artifact.path for artifact in metadata),
    )
    metadata_root = _canonical_root(verifier.ensure_startup_metadata())
    config_path = verifier.ensure_path("config.json", allowed_roles={"config"})
    _verified_config_document(config_path, configs[0])
    # An exact file path prevents Transformers from selecting another local
    # config file. Remote code is forbidden even if auto_map is declared.
    config = AutoDistributedConfig.from_pretrained(
        config_path, local_files_only=True, trust_remote_code=False, token=False, dht_prefix=manifest.dht_prefix
    )
    _verified_config_document(config_path, configs[0])
    manifest.validate_model_config(config)
    if manifest.runtime.attention_implementation != "auto":
        config._attn_implementation = manifest.runtime.attention_implementation
    weight_map = verifier.load_weight_map()
    select_manifest_block_artifacts(
        manifest,
        block_prefix=config.block_prefix,
        start_block=0,
        end_block=manifest.model.num_blocks,
        weight_map=weight_map,
    )
    key = (manifest.digest_id, cache_root, metadata_root, str(selected_device), attn_cache_tokens, drift.__version__)
    profile = None if profile_cache is None else profile_cache._get(key)
    if profile is None:
        profile = build_model_memory_profile(
            config, dtype=dtype, quant_type=quant_type, attn_cache_tokens=attn_cache_tokens
        )
        if profile_cache is not None:
            profile_cache._put(key, profile)
    return PlacementModelMetadata(
        manifest_digest=manifest.digest_id,
        cache_root=cache_root,
        metadata_root=metadata_root,
        block_prefix=config.block_prefix,
        weight_map=weight_map,
        memory_profile=profile,
        device=str(selected_device),
        native_quantization_probe_required=quant_type in (QuantType.INT8, QuantType.NF4),
    )
