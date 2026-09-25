"""One bounded offline CUDA llama.cpp/Qwen3 local server smoke.

Requires the two verified release archives unpacked by
``prepare_llama_b11173_local.py`` and a local converted F16 GGUF. It binds only
loopback, uses a temporary API key, issues synthetic completions, and confirms
the owned process tree has stopped. This is not multi-GPU or exact-model proof.
"""

# isort: skip_file

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.node.edge_supervisor import _force_containment_exit, _new_containment  # noqa: E402
from drift.inference_provider import (  # noqa: E402
    Availability, EventKind, InferenceLimits, InferenceRequest, ProviderIdentity, ProviderProfile,
)
from drift.managed_llama_cpp import ManagedLlamaCppAdapter, ManagedLlamaCppBinding  # noqa: E402
from drift.managed_vllm import ManagedGenerationOptions  # noqa: E402
from drift.managed_vllm_text import ManagedVllmTextClient  # noqa: E402
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime  # noqa: E402
from drift.api.server import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / ".gate13-runs" / "llama-b11173"
SERVER = ROOT / "run" / "llama-server.exe"
MODEL = ROOT / "qwen3-17b-f16.gguf"
LOG = ROOT / "smoke-server.log"
SERVER_SHA256 = "925ea12e72149baba11041f77394b6e07951eac6693118762db882b78687172e"
MODEL_SHA256 = "11612b8bbfbbe59d484541a6bcda2a8d84632600fdfbe00d2290c726bb6cbf7d"
ALIAS = "test/qwen3-1.7b-llama-b11173"
PROMPT = "Reply with one short greeting."
STARTUP_SECONDS = 90


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            result.update(block)
    return result.hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def gpu_memory_mib() -> int:
    output = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.strip().splitlines()
    if len(output) != 1 or not output[0].strip().isdecimal():
        raise RuntimeError("single local GPU memory report is unavailable")
    return int(output[0].strip())


def main() -> int:
    if not SERVER.is_file() or sha256(SERVER) != SERVER_SHA256:
        raise RuntimeError("pinned llama-server executable is absent or changed")
    if not MODEL.is_file() or sha256(MODEL) != MODEL_SHA256:
        raise RuntimeError("converted local Qwen GGUF is absent or changed")
    version = subprocess.run([str(SERVER), "--version"], capture_output=True, text=True, timeout=10)
    if version.returncode or "build 11173, commit 84e76d8a2" not in version.stdout + version.stderr:
        raise RuntimeError("llama.cpp runtime version mismatch")

    port = free_port()
    baseline_gpu_memory = gpu_memory_mib()
    key = secrets.token_urlsafe(24)
    containment = _new_containment()
    process = None
    result = None
    try:
        with tempfile.TemporaryDirectory(prefix="communityai-llama-smoke-") as temporary:
            key_file = Path(temporary) / "key.txt"
            key_file.write_text(key + "\n", encoding="ascii")
            command = [
                str(SERVER), "--model", str(MODEL), "--alias", ALIAS,
                "--host", "127.0.0.1", "--port", str(port),
                "--api-key-file", str(key_file), "--no-ui", "--no-slots",
                "--device", "CUDA0", "--split-mode", "none", "--gpu-layers", "all",
                "--fit", "off", "--ctx-size", "256", "--parallel", "1",
                "--flash-attn", "off", "--ignore-eos", "--no-cache-prompt",
                "--verbosity", "4",
            ]
            environment = os.environ.copy()
            environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            with LOG.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command, cwd=str(ROOT / "run"), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, shell=False, env=environment,
                    **containment.popen_kwargs(),
                )
                containment.attach(process)
                containment.resume(process)
                base = f"http://127.0.0.1:{port}"
                headers = {"Authorization": "Bearer " + key}
                with httpx.Client(trust_env=False, timeout=3) as client:
                    deadline = time.monotonic() + STARTUP_SECONDS
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError("llama-server exited before readiness")
                        try:
                            health = client.get(base + "/health", headers=headers)
                            if health.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(1)
                    else:
                        raise TimeoutError("llama-server startup exceeded 90 seconds")
                    loaded_gpu_memory = gpu_memory_mib()
                    models = client.get(base + "/v1/models", headers=headers).json()
                    props = client.get(base + "/props", headers=headers).json()
                    if (
                        models.get("object") != "list" or len(models.get("data", [])) != 1
                        or models["data"][0].get("id") != ALIAS
                        or props.get("model_path") != str(MODEL)
                        or props.get("total_slots") != 1
                    ):
                        raise RuntimeError("llama.cpp readiness metadata mismatch")
                    runs = []
                    for limit in (4, 16, 16, 16):
                        began = time.monotonic()
                        response = client.post(
                            base + "/v1/completions", headers=headers,
                            json={"model": ALIAS, "prompt": PROMPT, "max_tokens": limit,
                                  "temperature": 0, "stream": False}, timeout=20,
                        )
                        seconds = time.monotonic() - began
                        response.raise_for_status()
                        body = response.json()
                        usage = body.get("usage")
                        choices = body.get("choices")
                        if (
                            type(usage) is not dict or type(choices) is not list or len(choices) != 1
                            or type(choices[0]) is not dict or not choices[0].get("text")
                            or type(usage.get("prompt_tokens")) is not int
                            or usage.get("completion_tokens") != limit
                        ):
                            raise RuntimeError("llama-server completion or usage mismatch")
                        runs.append({"output_tokens": limit, "seconds": round(seconds, 3),
                                     "tokens_per_second_including_prefill": round(limit / seconds, 3)})
                    stream_frames = []
                    stream_done = False
                    with client.stream(
                        "POST", base + "/v1/completions", headers=headers,
                        json={"model": ALIAS, "prompt": PROMPT, "max_tokens": 4,
                              "temperature": 0, "stream": True,
                              "stream_options": {"include_usage": True}}, timeout=20,
                    ) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if not line:
                                continue
                            if not line.startswith("data: "):
                                raise RuntimeError("unexpected llama.cpp SSE field")
                            payload = line[6:]
                            if payload == "[DONE]":
                                stream_done = True
                                break
                            if len(payload) > 1 << 20 or len(stream_frames) >= 100:
                                raise RuntimeError("llama.cpp SSE output exceeds smoke bound")
                            frame = json.loads(payload)
                            if type(frame) is not dict:
                                raise RuntimeError("invalid llama.cpp SSE frame")
                            stream_frames.append(frame)
                    if not stream_done or not stream_frames:
                        raise RuntimeError("llama.cpp SSE did not terminate")
                    profile = ProviderProfile(
                        "test/qwen-llama-local", ALIAS, Availability.AVAILABLE,
                        qualification_id="1" * 64,
                    )
                    binding = ManagedLlamaCppBinding(profile, ALIAS, base, key, (0,), "none", 256)
                    now = time.monotonic()
                    request = InferenceRequest(
                        ProviderIdentity("test/local-provider", "test/local-instance"),
                        profile.profile_id, profile.model_id, secrets.token_hex(16),
                        secrets.token_hex(16), now, now + 20, PROMPT,
                        InferenceLimits(252, 4, 100, 4096),
                    )

                    async def check_adapter():
                        adapter = ManagedLlamaCppAdapter(binding)
                        return [event async for event in adapter.stream(
                            request, options=ManagedGenerationOptions(temperature=0)
                        )]

                    adapter_events = asyncio.run(check_adapter())
                    if (
                        not adapter_events or adapter_events[-1].kind is not EventKind.COMPLETED
                        or adapter_events[-1].usage is None
                        or adapter_events[-1].usage.output_units != 4
                        or not any(event.kind is EventKind.OUTPUT for event in adapter_events)
                    ):
                        raise RuntimeError("CommunityAI llama.cpp adapter did not accept real stream")
                    digest = "sha256:" + "c" * 64
                    manager = ModelManager()
                    bridge = ManagedVllmTextClient(
                        ManagedLlamaCppAdapter(binding), request.identity, digest,
                    )
                    manager.register(
                        ModelDescriptor(ALIAS, manifest_digest=digest),
                        lambda: ModelRuntime(model=None, tokenizer=None, text_client=bridge),
                    )
                    app = create_app(model_manager=manager, api_keys=["local-api-key"], request_timeout=20.0)
                    with TestClient(app) as app_client:
                        api_response = app_client.post(
                            "/v1/completions",
                            json={"model": ALIAS, "prompt": PROMPT, "max_tokens": 4,
                                  "temperature": 0, "stream": False},
                            headers={"Authorization": "Bearer local-api-key"},
                        )
                        if api_response.status_code != 200:
                            raise RuntimeError("CommunityAI API to llama.cpp failed: " + api_response.text[:300])
                        api_body = api_response.json()
                        if (
                            not api_body["choices"][0]["text"]
                            or api_body["usage"]["completion_tokens"] != 4
                        ):
                            raise RuntimeError("CommunityAI API completion or usage mismatch")
                    result = {"model": "Qwen/Qwen3-1.7B", "backend": "llama.cpp b11173 CUDA",
                              "gpu_count_used": 1, "runs": runs,
                              "median_16_token_seconds": round(statistics.median(run["seconds"] for run in runs[1:]), 3),
                              "gpu_memory_delta_mib": loaded_gpu_memory - baseline_gpu_memory,
                              "sse_frames": len(stream_frames),
                              "communityai_adapter_completed": True,
                              "communityai_api_completed": True,
                              "props_build_info": props.get("build_info"),
                              "sse_models": sorted({str(frame.get("model")) for frame in stream_frames}),
                              "sse_final_usage": any(type(frame.get("usage")) is dict for frame in stream_frames),
                              "sse_finish_reasons": sorted({
                                  str(choice.get("finish_reason"))
                                  for frame in stream_frames for choice in frame.get("choices", [])
                                  if type(choice) is dict and choice.get("finish_reason") is not None
                              })}
    except BaseException:
        if LOG.exists():
            print(LOG.read_text(encoding="utf-8", errors="replace")[-2500:], file=sys.stderr)
        raise
    finally:
        try:
            if process is not None and not _force_containment_exit(containment, process, 0.02):
                raise RuntimeError("llama-server process-tree stop is unconfirmed")
        finally:
            containment.close()
    log = LOG.read_text(encoding="utf-8", errors="replace")
    offload_logged = "CUDA0" in log and "offloaded" in log
    if not offload_logged and result["gpu_memory_delta_mib"] < 2048:
        raise RuntimeError("GPU model offload is not confirmed by log or allocation")
    result["gpu_offload_log_confirmed"] = offload_logged
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
