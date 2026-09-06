"""Verified standalone inference with finite context, residency and generation budgets."""

from __future__ import annotations

import gc
import threading
from typing import Callable

from drift.model_manifest import ManifestArtifactVerifier, ManifestError, ModelManifest
from drift.node.config import NodeModelConfig
from drift.node.model_manager import ModelRuntime

_GIB = 1024**3
_WEIGHT_ROLES = {"weight", "quantized_weight", "converted_weight"}


def local_weight_bytes(manifest: ModelManifest) -> int:
    if manifest.runtime.quantization != "none":
        raise ManifestError("Standalone execution requires an explicitly qualified unquantized manifest")
    if manifest.runtime.adapter_profile != "none":
        raise ManifestError("Standalone adapters are not supported")
    return sum(artifact.size for artifact in manifest.artifacts_for_roles(_WEIGHT_ROLES))


def local_device(config: NodeModelConfig, manifest: ModelManifest) -> str:
    """Admit against available memory before any weight download or CUDA allocation."""
    import psutil
    import torch

    weights = local_weight_bytes(manifest)
    # The reserve covers temporary activations/cache and allocator workspace. Admission
    # is also checked against actual resident bytes after loading and per generation.
    required = weights + _GIB
    if required > config.local_max_memory_bytes:
        raise MemoryError("The local model and runtime reserve exceed the configured memory budget")
    if sum(a.size for a in manifest.artifacts) > config.local_max_disk_bytes:
        raise OSError("The verified local model exceeds the configured download/storage budget")
    requested = config.local_device
    if requested != "cpu" and torch.cuda.is_available():
        device = "cuda:0" if requested == "auto" else str(torch.device(requested))
        free, _total = torch.cuda.mem_get_info(device)
        if free >= required + 256 * 1024**2:
            return device
        if requested != "auto":
            raise MemoryError("Insufficient free GPU memory for the local model")
    elif requested.startswith("cuda"):
        raise RuntimeError("The configured local CUDA device is unavailable")
    if psutil.virtual_memory().available < required + 256 * 1024**2:
        raise MemoryError("Insufficient available RAM for the local model")
    return "cpu"


def local_route_observer(manifest: ModelManifest, config: NodeModelConfig) -> Callable[[], dict]:
    def observe() -> dict:
        try:
            device = local_device(config, manifest)
        except (RuntimeError, ValueError, OSError, MemoryError) as exc:
            return {"status": "unavailable", "source": "local", "last_error": str(exc)}
        return {
            "status": "complete",
            "source": "local",
            "device": device,
            "total_blocks": manifest.model.num_blocks,
            "covered_blocks": manifest.model.num_blocks,
            "missing_blocks": [],
            "minimum_replicas": 1,
            "replica_counts": [1] * manifest.model.num_blocks,
            "peer_count": 0,
            "last_updated_age": 0.0,
        }

    return observe


class LocalInferenceModel:
    """Own model residency and serialize bounded local generations."""

    def __init__(
        self, model, config: NodeModelConfig, manifest: ModelManifest, device: str, *, previous_cuda_fraction=None
    ):
        self._model = model
        self.config = config
        self.manifest = manifest
        self.device = device
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._previous_cuda_fraction = previous_cuda_fraction

    @property
    def generation_token_limit(self):
        return self.config.local_max_new_tokens

    def validate_generation(self, input_ids, kwargs):
        new_tokens = kwargs.get("max_new_tokens", self.config.local_max_new_tokens)
        if type(new_tokens) is not int or not 1 <= new_tokens <= self.config.local_max_new_tokens:
            raise ValueError("Requested generation exceeds the local token budget")
        context_limit = min(self.config.local_max_context, self.manifest.model.context_length)
        if input_ids.ndim != 2 or input_ids.shape[0] != 1 or input_ids.shape[1] + new_tokens > context_limit:
            raise ValueError("Requested conversation exceeds the local context budget")

    def generate(self, input_ids, *, streamer=None, **kwargs):
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList

        owner = self

        class StopOnClose(StoppingCriteria):
            def __call__(self, input_ids, scores, **unused):
                return owner._closed.is_set()

        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("Local runtime is closed")
            self.validate_generation(input_ids, kwargs)
            kwargs.setdefault("max_new_tokens", self.config.local_max_new_tokens)
            kwargs["max_time"] = self.config.local_max_seconds
            kwargs["stopping_criteria"] = StoppingCriteriaList([*kwargs.get("stopping_criteria", ()), StopOnClose()])
            with torch.inference_mode():
                output = self._model.generate(input_ids.to(self.device), streamer=streamer, **kwargs)
            return output.cpu()

    def close(self):
        import torch

        self._closed.set()
        with self._lock:
            self._model = None
            gc.collect()
            if self.device.startswith("cuda"):
                with torch.cuda.device(self.device):
                    torch.cuda.empty_cache()
                    if self._previous_cuda_fraction is not None:
                        torch.cuda.set_per_process_memory_fraction(self._previous_cuda_fraction, self.device)
                        self._previous_cuda_fraction = None

    def route_health(self):
        import torch

        result = {
            "status": "unavailable" if self._closed.is_set() else "complete",
            "source": "local",
            "device": self.device,
            "total_blocks": self.manifest.model.num_blocks,
            "covered_blocks": self.manifest.model.num_blocks,
            "peer_count": 0,
            "last_updated_age": 0.0,
        }
        if self.device.startswith("cuda") and not self._closed.is_set():
            result["memory"] = {
                "budget_bytes": self.config.local_max_memory_bytes,
                "allocated_bytes": torch.cuda.memory_allocated(self.device),
                "reserved_bytes": torch.cuda.memory_reserved(self.device),
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(self.device),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(self.device),
            }
        return result


def make_local_manifest_loader(manifest: ModelManifest, config: NodeModelConfig) -> Callable[[], ModelRuntime]:
    def load():
        import torch
        from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer

        device = local_device(config, manifest)
        verifier = ManifestArtifactVerifier(
            manifest,
            repository=manifest.source.repository,
            revision=manifest.source.revision,
            token=False,
            cache_dir=str(config.cache_dir) if config.cache_dir else None,
        )
        verifier.ensure_startup_metadata(include_tokenizer=True)
        for artifact in manifest.artifacts:
            verifier.ensure_path(artifact.path)
        stock_config = AutoConfig.from_pretrained(
            verifier.snapshot_root, local_files_only=True, trust_remote_code=False
        )
        model_class = (
            AutoModelForImageTextToText if getattr(stock_config, "text_config", None) else AutoModelForCausalLM
        )
        model = None
        previous_fraction = None
        try:
            if device.startswith("cuda"):
                previous_fraction = torch.cuda.get_per_process_memory_fraction(device)
                total = torch.cuda.get_device_properties(device).total_memory
                limit = (torch.cuda.memory_reserved(device) + config.local_max_memory_bytes) / total
                torch.cuda.set_per_process_memory_fraction(min(previous_fraction, limit), device)
            model = model_class.from_pretrained(
                verifier.snapshot_root,
                config=stock_config,
                local_files_only=True,
                trust_remote_code=False,
                torch_dtype=getattr(torch, manifest.runtime.dtype),
                attn_implementation=manifest.runtime.attention_implementation,
                device_map={"": device},
            ).eval()
            if model.get_memory_footprint() + _GIB > config.local_max_memory_bytes:
                raise MemoryError("Loaded local model exceeds the configured resident-memory budget")
            tokenizer = AutoTokenizer.from_pretrained(
                verifier.snapshot_root, local_files_only=True, trust_remote_code=False
            )
            owned = LocalInferenceModel(model, config, manifest, device, previous_cuda_fraction=previous_fraction)
            return ModelRuntime(model=owned, tokenizer=tokenizer, close=owned.close, route_health=owned.route_health)
        except BaseException:
            model = None
            gc.collect()
            if device.startswith("cuda"):
                torch.cuda.empty_cache()
                if previous_fraction is not None:
                    torch.cuda.set_per_process_memory_fraction(previous_fraction, device)
            raise

    return load
