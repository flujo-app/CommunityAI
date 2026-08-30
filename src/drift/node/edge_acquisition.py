"""Privacy-safe, resumable acquisition of exact client-selected manifest artifacts."""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

import drift
from drift.client.from_pretrained import select_checkpoint_shards
from drift.model_manifest import (
    ManifestArtifact,
    ManifestArtifactVerifier,
    ManifestError,
    ManifestTransferInterrupted,
    ModelManifest,
)
from drift.node.edge_benchmark import cache_is_empty, directory_size

EDGE_ACQUISITION_SCHEMA_VERSION = 1
_STARTUP_ROLES = {"chat_template", "config", "tokenizer", "weight_index"}
_CHECKPOINT_ROLES = {"weight"}


def _resolve_client_ignore_patterns(snapshot_root: Path) -> Sequence[str]:
    """Resolve the exact distributed CausalLM class without instantiating its tensors."""
    from transformers import AutoConfig

    from drift.utils.auto_config import _CLASS_MAPPING

    config = AutoConfig.from_pretrained(snapshot_root, local_files_only=True)
    model_type = config.model_type
    if model_type not in _CLASS_MAPPING:
        text_config = config.get_text_config() if hasattr(config, "get_text_config") else config
        if text_config is config or text_config.model_type not in _CLASS_MAPPING:
            raise ManifestError(f"DRIFT-LLM does not support model type {config.model_type}")
        model_type = text_config.model_type
    model_class = _CLASS_MAPPING[model_type].model_for_causal_lm
    if model_class is None:
        raise ManifestError(f"DRIFT-LLM has no distributed CausalLM for model type {model_type}")
    patterns = getattr(model_class, "_keys_to_ignore_on_load_unexpected", None)
    if not isinstance(patterns, (list, tuple)) or not all(isinstance(pattern, str) for pattern in patterns):
        raise ManifestError(f"Distributed CausalLM for model type {model_type} has no client shard selection policy")
    return tuple(patterns)


def _selected_weight_artifacts(
    manifest: ModelManifest,
    verifier: ManifestArtifactVerifier,
    ignored_key_patterns: Sequence[str],
) -> Sequence[ManifestArtifact]:
    indices = manifest.artifacts_for_roles({"weight_index"})
    if not indices:
        weights = manifest.artifacts_for_roles(_CHECKPOINT_ROLES)
        if not weights:
            raise ManifestError("Manifest has no checkpoint artifacts")
        return weights
    if len(indices) != 1:
        raise ManifestError("Manifest must declare exactly one checkpoint index")

    index_path = verifier.ensure_path(indices[0].path, allowed_roles={"weight_index"})
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        weight_map = index["weight_map"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ManifestError(f"Could not read verified checkpoint index: {type(exc).__name__}") from exc

    try:
        selected_paths = select_checkpoint_shards(weight_map, list(ignored_key_patterns))
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    selected = []
    for path in selected_paths:
        artifact = manifest.get_artifact(path)
        if artifact.role not in _CHECKPOINT_ROLES:
            raise ManifestError(f"Checkpoint index selected non-weight artifact {path!r}")
        selected.append(artifact)
    return tuple(selected)


def _safe_artifact_record(artifact: ManifestArtifact, transfer: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "path": artifact.path,
        "role": artifact.role,
        "size_bytes": artifact.size,
        "sha256": artifact.sha256,
        "materialization_attempts": transfer["attempts"],
        "resumptions": transfer["resumptions"],
        "resumed_from_bytes": transfer["resumed_from_bytes"],
        "elapsed_seconds": transfer["elapsed_seconds"],
    }


def acquire_client_artifacts(
    manifest: ModelManifest,
    *,
    cache_dir: Path | str,
    token: Optional[str] = None,
    max_resumptions: int = 3,
    verifier: Optional[ManifestArtifactVerifier] = None,
    ignored_key_patterns: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Materialize the loader's exact client files and return a path/credential-free record."""
    if isinstance(max_resumptions, bool) or not isinstance(max_resumptions, int) or not 0 <= max_resumptions <= 3:
        raise ValueError("max_resumptions must be an integer between 0 and 3")

    cache_dir = Path(cache_dir)
    if not cache_is_empty(cache_dir):
        raise ManifestError("acquisition cache must be empty")
    cache_before = directory_size(cache_dir)
    started = time.perf_counter()
    verifier = verifier or ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        token=token,
        cache_dir=cache_dir,
    )
    if verifier.manifest.digest != manifest.digest:
        raise ManifestError("Artifact verifier manifest does not match acquisition manifest")
    if verifier.cache_dir is None or Path(verifier.cache_dir).absolute() != cache_dir.absolute():
        raise ManifestError("Artifact verifier must use the acquisition cache")

    transfers: Dict[str, Dict[str, Any]] = {}
    resumptions_used = 0

    def materialize(artifact: ManifestArtifact, *, allowed_roles: Iterable[str]) -> Path:
        nonlocal resumptions_used
        existing = transfers.get(artifact.path)
        if existing is not None and existing.get("verified") is True:
            return verifier.ensure_path(artifact.path, allowed_roles=allowed_roles)

        attempt_started = time.perf_counter()
        attempts = 0
        resumptions = 0
        resumed_from_bytes = []
        while True:
            partial_before = verifier.partial_size(artifact.path)
            attempts += 1
            if attempts > 1 and partial_before:
                resumed_from_bytes.append(partial_before)
            try:
                resolved = verifier.ensure_path(artifact.path, allowed_roles=allowed_roles)
                break
            except ManifestTransferInterrupted:
                if resumptions_used >= max_resumptions:
                    raise
                resumptions_used += 1
                resumptions += 1
        if verifier.partial_size(artifact.path):
            raise ManifestError(f"Verified artifact {artifact.path} retained a partial transfer")
        transfers[artifact.path] = {
            "attempts": attempts,
            "resumptions": resumptions,
            "resumed_from_bytes": resumed_from_bytes,
            "elapsed_seconds": time.perf_counter() - attempt_started,
            "verified": True,
        }
        return resolved

    startup_artifacts = manifest.artifacts_for_roles(_STARTUP_ROLES)
    if not any(artifact.role == "config" for artifact in startup_artifacts):
        raise ManifestError("Manifest has no configuration artifact")
    for artifact in startup_artifacts:
        materialize(artifact, allowed_roles=_STARTUP_ROLES)

    patterns = (
        tuple(ignored_key_patterns)
        if ignored_key_patterns is not None
        else tuple(_resolve_client_ignore_patterns(verifier.snapshot_root))
    )
    if not all(isinstance(pattern, str) for pattern in patterns):
        raise ValueError("ignored_key_patterns must contain only strings")
    weight_artifacts = _selected_weight_artifacts(manifest, verifier, patterns)
    for artifact in weight_artifacts:
        materialize(artifact, allowed_roles=_CHECKPOINT_ROLES)

    selected = sorted(
        {artifact.path: artifact for artifact in (*startup_artifacts, *weight_artifacts)}.values(),
        key=lambda item: item.path,
    )
    cache_after = directory_size(cache_dir)
    elapsed = time.perf_counter() - started
    return {
        "schema_version": EDGE_ACQUISITION_SCHEMA_VERSION,
        "acquired_at_unix": int(time.time()),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "drift": drift.__version__,
        },
        "model": {
            "id": manifest.name,
            "manifest_digest": manifest.digest_id,
            "repository": manifest.source.repository,
            "revision": manifest.source.revision,
            "dtype": manifest.runtime.dtype,
        },
        "selection": {
            "startup_artifact_paths": sorted(artifact.path for artifact in startup_artifacts),
            "weight_artifact_paths": sorted(artifact.path for artifact in weight_artifacts),
            "artifact_count": len(selected),
            "artifact_bytes": sum(artifact.size for artifact in selected),
            "weight_artifact_bytes": sum(artifact.size for artifact in weight_artifacts),
        },
        "artifacts": [_safe_artifact_record(artifact, transfers[artifact.path]) for artifact in selected],
        "transfer": {
            "elapsed_seconds": elapsed,
            "max_resumptions": max_resumptions,
            "resumptions": resumptions_used,
            "completed": True,
        },
        "storage": {
            "cold_start": True,
            "cache_bytes_before": cache_before,
            "cache_bytes_after": cache_after,
            "cache_growth_bytes": max(0, cache_after - cache_before),
            "verified": True,
        },
        "privacy": {
            "credentials_retained": False,
            "local_paths_retained": False,
            "response_bodies_retained": False,
            "urls_retained": False,
        },
    }
