"""Reproducible resource measurements for a manifested client-only runtime."""

from __future__ import annotations

import platform
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import drift
from drift.model_manifest import ModelManifest
from drift.node.model_manager import ModelRuntime

EDGE_BENCHMARK_SCHEMA_VERSION = 1


def directory_size(path: Path | str) -> int:
    """Return bytes in regular, non-symlink files below *path*."""
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for candidate in root.rglob("*"):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
        except OSError:
            # Concurrent cache maintenance may remove a temporary between the walk and stat.
            continue
    return total


def cache_is_empty(path: Path | str) -> bool:
    root = Path(path)
    return not root.exists() or next(root.iterdir(), None) is None


def _storage_key(parameter) -> tuple:
    try:
        storage = parameter.untyped_storage()
        return str(parameter.device), storage.data_ptr(), storage.nbytes()
    except (AttributeError, RuntimeError):
        return str(parameter.device), parameter.data_ptr(), parameter.numel() * parameter.element_size()


def _component_memory(modules: Iterable[tuple[str, Any]]) -> Dict[str, Any]:
    components: Dict[str, Any] = {}
    all_storages: Dict[tuple, int] = {}
    for name, module in modules:
        storages: Dict[tuple, int] = {}
        parameters = tuple(module.parameters())
        for parameter in parameters:
            key = _storage_key(parameter)
            storages[key] = key[2]
            all_storages[key] = key[2]
        components[name] = {
            "parameter_count": sum(parameter.numel() for parameter in parameters),
            "parameter_bytes": sum(storages.values()),
            "devices": sorted({str(parameter.device) for parameter in parameters}),
            "dtypes": sorted({str(parameter.dtype).removeprefix("torch.") for parameter in parameters}),
        }
    return {"components": components, "unique_parameter_bytes": sum(all_storages.values())}


def client_component_memory(model) -> Dict[str, Any]:
    """Measure the local embedding/head weights, de-duplicating tied storage."""
    return _component_memory(
        (
            ("input_embeddings", model.get_input_embeddings()),
            ("output_head", model.get_output_embeddings()),
        )
    )


class _TokenTimingStreamer:
    def __init__(self) -> None:
        self._saw_prompt = False
        self.first_token_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.generated_tokens = 0

    def put(self, value) -> None:
        # Transformers sends the prompt as the first value when generation starts.
        if not self._saw_prompt:
            self._saw_prompt = True
            return
        count = int(value.numel())
        if count and self.first_token_at is None:
            self.first_token_at = time.perf_counter()
        self.generated_tokens += count

    def end(self) -> None:
        self.ended_at = time.perf_counter()


def _accelerator_allocated_bytes(torch_module) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for name in ("cuda", "xpu"):
        backend = getattr(torch_module, name, None)
        if backend is None:
            continue
        try:
            available = backend.is_available()
        except (AttributeError, RuntimeError):
            available = False
        if available and callable(getattr(backend, "memory_allocated", None)):
            try:
                result[name] = int(backend.memory_allocated())
            except RuntimeError:
                pass
    mps = getattr(torch_module, "mps", None)
    if mps is not None:
        try:
            if mps.is_available() and callable(getattr(mps, "current_allocated_memory", None)):
                result["mps"] = int(mps.current_allocated_memory())
        except (AttributeError, RuntimeError):
            pass
    return result


class _ResourceSampler:
    """Sample the complete client process tree and accelerator allocations."""

    def __init__(self, torch_module, *, interval: float = 0.02) -> None:
        try:
            import psutil
        except ImportError as exc:
            raise RuntimeError("edge benchmarking requires psutil; install drift[benchmark]") from exc
        self._psutil = psutil
        self._process = psutil.Process()
        self._torch = torch_module
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.peak_rss_bytes = 0
        self.peak_accelerator_bytes: Dict[str, int] = {}

    def _rss(self) -> int:
        processes = [self._process]
        try:
            processes.extend(self._process.children(recursive=True))
        except self._psutil.Error:
            pass
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except self._psutil.Error:
                continue
        return total

    def sample(self) -> tuple[int, Dict[str, int]]:
        rss = self._rss()
        accelerator = _accelerator_allocated_bytes(self._torch)
        self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
        for name, value in accelerator.items():
            self.peak_accelerator_bytes[name] = max(self.peak_accelerator_bytes.get(name, 0), value)
        return rss, accelerator

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.sample()

    def start(self) -> tuple[int, Dict[str, int]]:
        baseline = self.sample()
        self._thread = threading.Thread(target=self._run, name="drift-edge-resource-sampler", daemon=True)
        self._thread.start()
        return baseline

    def close(self) -> None:
        self.sample()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


def benchmark_client_runtime(
    manifest: ModelManifest,
    loader: Callable[[], ModelRuntime],
    *,
    cache_dir: Path | str,
    prompt: str = "Hello",
    max_new_tokens: int = 8,
) -> Dict[str, Any]:
    """Load, generate through, close, and report one exact manifested client runtime."""
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or max_new_tokens < 2:
        raise ValueError("max_new_tokens must be an integer >= 2")

    import torch

    cache_dir = Path(cache_dir)
    cache_before = directory_size(cache_dir)
    cold_start = cache_is_empty(cache_dir)
    sampler = _ResourceSampler(torch)
    baseline_rss, accelerator_baseline = sampler.start()
    runtime: Optional[ModelRuntime] = None
    loaded_rss = baseline_rss
    accelerator_loaded = accelerator_baseline
    try:
        load_started = time.perf_counter()
        runtime = loader()
        load_seconds = time.perf_counter() - load_started
        loaded_rss, accelerator_loaded = sampler.sample()
        local_components = client_component_memory(runtime.model)

        tokenized = runtime.tokenizer(prompt, return_tensors="pt")
        input_ids = tokenized["input_ids"] if isinstance(tokenized, dict) else tokenized.input_ids
        prompt_tokens = int(input_ids.shape[-1])
        streamer = _TokenTimingStreamer()
        generation_started = time.perf_counter()
        with torch.inference_mode():
            outputs = runtime.model.generate(
                input_ids,
                streamer=streamer,
                max_new_tokens=max_new_tokens,
                min_new_tokens=max_new_tokens,
                do_sample=False,
            )
        generation_ended = time.perf_counter()
        if streamer.first_token_at is None:
            raise RuntimeError("the model completed without emitting a first token to the benchmark streamer")
        generated_tokens = int(outputs.shape[-1]) - prompt_tokens
        if generated_tokens < 1:
            raise RuntimeError("the model completed without generating any tokens")
        first_token_seconds = streamer.first_token_at - generation_started
        decode_ended = streamer.ended_at if streamer.ended_at is not None else generation_ended
        decode_seconds = max(0.0, decode_ended - streamer.first_token_at)
        decode_tokens = max(0, generated_tokens - 1)
        sampler.sample()
        output_ids = outputs[0].detach().cpu().tolist()
        decoded = runtime.tokenizer.decode(outputs[0], skip_special_tokens=False)
    finally:
        try:
            if runtime is not None and runtime.close is not None:
                runtime.close()
        finally:
            sampler.close()

    cache_after = directory_size(cache_dir)
    accelerator_names = sorted(
        set(accelerator_baseline) | set(accelerator_loaded) | set(sampler.peak_accelerator_bytes)
    )
    return {
        "schema_version": EDGE_BENCHMARK_SCHEMA_VERSION,
        "measured_at_unix": int(time.time()),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "drift": drift.__version__,
            "torch": torch.__version__,
        },
        "model": {
            "id": manifest.name,
            "manifest_digest": manifest.digest_id,
            "repository": manifest.source.repository,
            "revision": manifest.source.revision,
            "dtype": manifest.runtime.dtype,
        },
        "workload": {
            "prompt": prompt,
            "prompt_tokens": prompt_tokens,
            "requested_new_tokens": max_new_tokens,
            "generated_tokens": generated_tokens,
            "output_ids": output_ids,
            "decoded": decoded,
        },
        "storage": {
            "cold_start": cold_start,
            "cache_bytes_before": cache_before,
            "cache_bytes_after": cache_after,
            "cache_growth_bytes": max(0, cache_after - cache_before),
        },
        "memory": {
            "process_tree_rss_baseline_bytes": baseline_rss,
            "process_tree_rss_loaded_bytes": loaded_rss,
            "process_tree_rss_peak_bytes": sampler.peak_rss_bytes,
            "process_tree_rss_peak_delta_bytes": max(0, sampler.peak_rss_bytes - baseline_rss),
            "accelerators": {
                name: {
                    "allocated_baseline_bytes": accelerator_baseline.get(name, 0),
                    "allocated_loaded_bytes": accelerator_loaded.get(name, 0),
                    "allocated_peak_bytes": sampler.peak_accelerator_bytes.get(name, 0),
                    "allocated_peak_delta_bytes": max(
                        0, sampler.peak_accelerator_bytes.get(name, 0) - accelerator_baseline.get(name, 0)
                    ),
                }
                for name in accelerator_names
            },
            "client_components": local_components,
        },
        "latency": {
            "load_seconds": load_seconds,
            "first_token_seconds": first_token_seconds,
            "total_generation_seconds": generation_ended - generation_started,
            "decode_seconds_after_first_token": decode_seconds,
            "decode_tokens_per_second": decode_tokens / decode_seconds if decode_tokens and decode_seconds else None,
        },
    }
