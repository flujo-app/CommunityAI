"""Reproducible resource measurements for a manifested client-only runtime."""

from __future__ import annotations

import gc
import platform
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import drift
from drift.model_manifest import ModelManifest
from drift.node.model_manager import ModelRuntime

EDGE_BENCHMARK_SCHEMA_VERSION = 2
POST_CLOSE_RSS_TOLERANCE_BYTES = 16 * 1024 * 1024
POST_CLOSE_CLEANUP_TIMEOUT_SECONDS = 5.0
POST_CLOSE_CLEANUP_POLL_INTERVAL_SECONDS = 0.05


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


def _release_accelerator_caches(torch_module) -> None:
    for name in ("cuda", "xpu", "mps"):
        backend = getattr(torch_module, name, None)
        empty_cache = getattr(backend, "empty_cache", None)
        if callable(empty_cache):
            try:
                empty_cache()
            except RuntimeError:
                pass


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

    def _process_tree(self) -> list:
        processes = [self._process]
        try:
            processes.extend(self._process.children(recursive=True))
        except self._psutil.Error:
            pass
        return processes

    def _rss(self) -> int:
        total = 0
        for process in self._process_tree():
            try:
                total += process.memory_info().rss
            except self._psutil.Error:
                continue
        return total

    def child_process_ids(self) -> set[int]:
        return {process.pid for process in self._process_tree()[1:]}

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
    cleanup_timeout_seconds: float = POST_CLOSE_CLEANUP_TIMEOUT_SECONDS,
    cleanup_poll_interval_seconds: float = POST_CLOSE_CLEANUP_POLL_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """Load, generate through, close, and report one exact manifested client runtime."""
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or max_new_tokens < 2:
        raise ValueError("max_new_tokens must be an integer >= 2")
    for name, value in (
        ("cleanup_timeout_seconds", cleanup_timeout_seconds),
        ("cleanup_poll_interval_seconds", cleanup_poll_interval_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{name} must be a non-negative number")

    import torch

    cache_dir = Path(cache_dir)
    cache_before = directory_size(cache_dir)
    cold_start = cache_is_empty(cache_dir)
    sampler = _ResourceSampler(torch)
    baseline_rss, accelerator_baseline = sampler.start()
    baseline_child_process_ids = sampler.child_process_ids()
    runtime: Optional[ModelRuntime] = None
    cleanup_observer: Optional[Callable[[], Dict[str, Any]]] = None
    tokenized = input_ids = outputs = None
    loaded_rss = baseline_rss
    accelerator_loaded = accelerator_baseline
    close_invoked = False
    close_error_type: Optional[str] = None
    route_manager_cleanup: Dict[str, Any] = {"observed": False, "clean": False}
    post_close_rss = baseline_rss
    accelerator_post_close = accelerator_baseline
    post_close_child_process_ids = set(baseline_child_process_ids)
    cleanup_samples = 0
    cleanup_elapsed_seconds = 0.0
    cleanup_deadline_reached = False
    try:
        load_started = time.perf_counter()
        runtime = loader()
        cleanup_observer = runtime.cleanup_health
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
                close_invoked = True
                runtime.close()
        except Exception as exc:
            close_error_type = type(exc).__name__
        finally:
            tokenized = input_ids = outputs = runtime = None
            gc.collect()
            _release_accelerator_caches(torch)
            cleanup_started = time.perf_counter()
            cleanup_deadline = cleanup_started + cleanup_timeout_seconds
            while True:
                cleanup_samples += 1
                if cleanup_observer is not None:
                    try:
                        route_manager_cleanup = dict(cleanup_observer())
                    except Exception as exc:
                        route_manager_cleanup = {
                            "observed": True,
                            "clean": False,
                            "error_type": type(exc).__name__,
                        }
                post_close_rss, accelerator_post_close = sampler.sample()
                post_close_child_process_ids = sampler.child_process_ids()
                route_clean = (
                    route_manager_cleanup.get("observed") is True and route_manager_cleanup.get("clean") is True
                )
                process_tree_clean = not (post_close_child_process_ids - baseline_child_process_ids)
                memory_clean = max(0, post_close_rss - baseline_rss) <= POST_CLOSE_RSS_TOLERANCE_BYTES
                accelerators_clean = all(
                    accelerator_post_close.get(name, 0) <= accelerator_baseline.get(name, 0)
                    for name in set(accelerator_baseline) | set(accelerator_post_close)
                )
                if route_clean and process_tree_clean and memory_clean and accelerators_clean:
                    break
                can_stabilize = close_invoked and close_error_type is None and cleanup_observer is not None
                remaining = cleanup_deadline - time.perf_counter()
                if not can_stabilize:
                    break
                if remaining <= 0:
                    cleanup_deadline_reached = True
                    break
                time.sleep(min(cleanup_poll_interval_seconds, remaining))
            cleanup_elapsed_seconds = time.perf_counter() - cleanup_started
            cleanup_observer = None
            gc.collect()
            _release_accelerator_caches(torch)
            post_close_rss, accelerator_post_close = sampler.sample()
            post_close_child_process_ids = sampler.child_process_ids()
            sampler.close()

    cache_after = directory_size(cache_dir)
    accelerator_names = sorted(
        set(accelerator_baseline)
        | set(accelerator_loaded)
        | set(sampler.peak_accelerator_bytes)
        | set(accelerator_post_close)
    )
    unexpected_child_processes = post_close_child_process_ids - baseline_child_process_ids
    additional_child_processes = len(unexpected_child_processes)
    accelerator_cleanup = {
        name: {
            "allocated_baseline_bytes": accelerator_baseline.get(name, 0),
            "allocated_post_close_bytes": accelerator_post_close.get(name, 0),
            "allocated_post_close_delta_bytes": max(
                0, accelerator_post_close.get(name, 0) - accelerator_baseline.get(name, 0)
            ),
            "clean": accelerator_post_close.get(name, 0) <= accelerator_baseline.get(name, 0),
        }
        for name in accelerator_names
    }
    runtime_close_clean = close_invoked and close_error_type is None
    route_manager_clean = route_manager_cleanup.get("observed") is True and route_manager_cleanup.get("clean") is True
    process_tree_clean = additional_child_processes == 0
    post_close_rss_delta = max(0, post_close_rss - baseline_rss)
    memory_clean = post_close_rss_delta <= POST_CLOSE_RSS_TOLERANCE_BYTES
    accelerators_clean = all(status["clean"] for status in accelerator_cleanup.values())
    cleanup_passed = (
        runtime_close_clean and route_manager_clean and process_tree_clean and memory_clean and accelerators_clean
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
            "process_tree_rss_post_close_bytes": post_close_rss,
            "process_tree_rss_post_close_delta_bytes": post_close_rss_delta,
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
        "cleanup": {
            "runtime_close": {
                "available": runtime_close_clean or close_invoked,
                "invoked": close_invoked,
                "error_type": close_error_type,
                "clean": runtime_close_clean,
            },
            "route_manager": route_manager_cleanup,
            "process_tree": {
                "child_processes_baseline": len(baseline_child_process_ids),
                "child_processes_post_close": len(post_close_child_process_ids),
                "additional_child_processes_post_close": additional_child_processes,
                "clean": process_tree_clean,
            },
            "memory": {
                "process_tree_rss_baseline_bytes": baseline_rss,
                "process_tree_rss_post_close_bytes": post_close_rss,
                "process_tree_rss_post_close_delta_bytes": post_close_rss_delta,
                "rss_tolerance_bytes": POST_CLOSE_RSS_TOLERANCE_BYTES,
                "clean": memory_clean,
            },
            "accelerators": {
                "devices": accelerator_cleanup,
                "clean": accelerators_clean,
            },
            "stabilization": {
                "timeout_seconds": cleanup_timeout_seconds,
                "poll_interval_seconds": cleanup_poll_interval_seconds,
                "samples": cleanup_samples,
                "elapsed_seconds": cleanup_elapsed_seconds,
                "deadline_reached": cleanup_deadline_reached,
            },
            "passed": cleanup_passed,
        },
    }
