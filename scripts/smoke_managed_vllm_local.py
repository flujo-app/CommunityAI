"""One bounded, offline, real-vLLM Qwen smoke on an existing local GPU.

The image and complete model snapshot must already be cached. This does not
qualify an advertised model, multi-GPU topology, or release package. It starts
one loopback Docker container, probes it, sends one synthetic completion, and
removes the container. No model download or broad test suite is performed.
"""

# isort: skip_file

from __future__ import annotations

import argparse
import asyncio
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import httpx

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import (  # noqa: E402
    Availability,
    EventKind,
    InferenceLimits,
    InferenceRequest,
    ProviderIdentity,
    ProviderProfile,
)
from drift.managed_vllm import ManagedVllmAdapter, ManagedVllmBinding  # noqa: E402
from drift.managed_vllm_probe import probe_managed_vllm  # noqa: E402

IMAGE = "vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40"
MODEL_REPOSITORY = "models--Qwen--Qwen3-1.7B"
MODEL_REVISION = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
SERVED = "test/qwen3-1.7b-local-vllm"
STARTUP_SECONDS = 180


def docker(*args: str, timeout: int = 15, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-1000:]}")
    return result


def unused_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


async def verify_backend(binding: ManagedVllmBinding) -> tuple[str, int, int]:
    await probe_managed_vllm(binding, expected_max_model_len=256, seconds=5)
    now = time.monotonic()
    request = InferenceRequest(
        ProviderIdentity("test/local-provider", "test/local-instance"),
        binding.profile.profile_id,
        binding.profile.model_id,
        secrets.token_hex(16),
        secrets.token_hex(16),
        now,
        now + 45,
        "Reply with one short greeting.",
        InferenceLimits(240, 16, 128, 4096),
    )
    events = [event async for event in ManagedVllmAdapter(binding).stream(request)]
    if not events or events[-1].kind is not EventKind.COMPLETED:
        raise RuntimeError(f"vLLM stream did not complete: {[event.kind.value for event in events]}")
    output = "".join(event.text for event in events if event.kind is EventKind.OUTPUT)
    usage = events[-1].usage
    if not output or usage is None or usage.output_units < 1:
        raise RuntimeError("vLLM returned no accepted output/usage")
    return output, usage.input_units, usage.output_units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", type=Path, required=True, help="existing Hugging Face hub cache root")
    args = parser.parse_args()
    hub = args.hub.resolve(strict=True)
    model_cache = hub / MODEL_REPOSITORY
    snapshot = model_cache / "snapshots" / MODEL_REVISION
    required = ("config.json", "tokenizer.json", "model.safetensors.index.json")
    if not snapshot.is_dir() or any(not (snapshot / name).is_file() for name in required):
        parser.error("complete local Qwen3-1.7B snapshot is required")
    if not list(snapshot.glob("model-*.safetensors")):
        parser.error("local model weight shards are missing")
    if docker("image", "inspect", IMAGE, check=False).returncode:
        parser.error("pinned vLLM image is not cached")

    name = "communityai-vllm-smoke-" + secrets.token_hex(4)
    port = unused_port()
    key = secrets.token_urlsafe(24)
    started = False
    with tempfile.TemporaryDirectory(prefix="communityai-vllm-smoke-") as folder:
        env_file = Path(folder) / "backend.env"
        env_file.write_text(
            f"VLLM_API_KEY={key}\nHF_HUB_OFFLINE=1\nTRANSFORMERS_OFFLINE=1\n"
            "VLLM_NO_USAGE_STATS=1\nCUDA_VISIBLE_DEVICES=0\n",
            encoding="ascii",
        )
        try:
            started = True
            docker(
                "run",
                "--detach",
                "--rm",
                "--gpus",
                "device=0",
                "--name",
                name,
                "--publish",
                f"127.0.0.1:{port}:8000",
                "--mount",
                f"type=bind,source={model_cache},target=/model-cache,readonly",
                "--env-file",
                str(env_file),
                "--entrypoint",
                "vllm",
                IMAGE,
                "serve",
                "/model-cache/snapshots/" + MODEL_REVISION,
                "--served-model-name",
                SERVED,
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--tensor-parallel-size",
                "1",
                "--pipeline-parallel-size",
                "1",
                "--distributed-executor-backend",
                "mp",
                "--max-model-len",
                "256",
                "--max-num-seqs",
                "1",
                "--gpu-memory-utilization",
                "0.72",
                "--dtype",
                "half",
                "--enforce-eager",
                "--no-enable-log-requests",
                "--disable-uvicorn-access-log",
                timeout=30,
            )
            profile = ProviderProfile("test/qwen-vllm-local", SERVED, Availability.AVAILABLE, qualification_id="1" * 64)
            binding = ManagedVllmBinding(profile, SERVED, f"http://127.0.0.1:{port}", key, (0,), 1, 1)
            deadline = time.monotonic() + STARTUP_SECONDS
            while time.monotonic() < deadline:
                state = docker("inspect", "--format", "{{.State.Running}}", name, check=False)
                if state.returncode or state.stdout.strip() != "true":
                    raise RuntimeError("vLLM container exited before readiness")
                try:
                    with httpx.Client(trust_env=False, timeout=2) as client:
                        response = client.get(binding.base_url + "/health", headers={"Authorization": "Bearer " + key})
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(2)
            else:
                raise TimeoutError("vLLM startup exceeded 180 seconds")
            output, input_units, output_units = asyncio.run(verify_backend(binding))
            print(f"real vLLM local smoke PASS: input={input_units}, output={output_units}, text={output!r}")
            return 0
        finally:
            if started:
                docker("rm", "--force", name, timeout=30, check=False)
                remaining = docker("inspect", name, timeout=10, check=False)
                if remaining.returncode == 0:
                    raise RuntimeError("vLLM smoke container cleanup is unconfirmed")


if __name__ == "__main__":
    raise SystemExit(main())
