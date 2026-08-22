"""Lazy construction and cleanup of manifested client runtimes."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional, Sequence

from hivemind.utils.logging import get_logger

from drift.model_manifest import ManifestArtifactVerifier, ModelManifest
from drift.node.model_manager import ModelRuntime
from drift.node.route_health import sequence_manager_route_health

logger = get_logger(__name__)


def make_manifest_loader(
    manifest: ModelManifest,
    *,
    initial_peers: Sequence[str],
    token: Optional[str] = None,
    cache_dir: Optional[str] = None,
    revocation_files: Sequence[str] = (),
    request_timeout: float = 30,
    max_retries: int = 3,
) -> Callable[[], ModelRuntime]:
    """Return a lazy loader pinned to one exact manifest and swarm namespace."""

    def load() -> ModelRuntime:
        import torch
        from transformers import AutoTokenizer

        from drift import AutoDistributedModelForCausalLM

        verifier = ManifestArtifactVerifier(
            manifest,
            repository=manifest.source.repository,
            revision=manifest.source.revision,
            token=token,
            cache_dir=cache_dir,
        )
        verifier.ensure_startup_metadata(include_tokenizer=True)
        logger.info(f"Loading tokenizer and client-side weights for {manifest.digest_id}")
        tokenizer = AutoTokenizer.from_pretrained(verifier.snapshot_root, local_files_only=True)
        model = AutoDistributedModelForCausalLM.from_pretrained(
            manifest.source.repository,
            initial_peers=list(initial_peers),
            dht_prefix=manifest.dht_prefix,
            revision=manifest.source.revision,
            manifest_digest=manifest.digest,
            manifest_execution_profile=manifest.runtime.to_dict(),
            revocation_files=[str(Path(path)) for path in revocation_files],
            torch_dtype=getattr(torch, manifest.runtime.dtype),
            token=token,
            cache_dir=cache_dir,
            request_timeout=request_timeout,
            max_retries=max_retries,
            max_backoff=5,
            artifact_verifier=verifier,
        )
        return ModelRuntime(
            model=model,
            tokenizer=tokenizer,
            close=_runtime_closer(model),
            route_health=_runtime_route_observer(model),
        )

    return load


def _runtime_closer(model) -> Callable[[], None]:
    """Build an idempotent closer for a distributed Transformers client."""
    lock = threading.Lock()
    closed = False

    def close() -> None:
        nonlocal closed
        with lock:
            if closed:
                return
            closed = True

        try:
            remote_layers = model.transformer.h
            sequence_manager = remote_layers.sequence_manager
        except AttributeError:
            logger.warning("Loaded model has no DRIFT sequence manager to shut down")
            return

        try:
            sequence_manager.shutdown()
        finally:
            dht = getattr(sequence_manager, "dht", None)
            if dht is not None and dht.is_alive():
                dht.shutdown()

    return close


def _runtime_route_observer(model) -> Callable[[], dict]:
    """Build a cheap observer over the loaded client's last routing snapshot."""
    remote_layers = model.transformer.h
    sequence_manager = remote_layers.sequence_manager

    def observe() -> dict:
        result = sequence_manager_route_health(sequence_manager)
        result["source"] = "runtime"
        result["last_error"] = None
        return result

    return observe
