"""Focused admission probe checks using an in-memory HTTP server, no ML import."""

# isort: skip_file

import asyncio
import sys
import types
from pathlib import Path

import httpx

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import Availability, ProviderProfile  # noqa: E402
from drift.managed_vllm import ManagedVllmBinding  # noqa: E402
from drift.managed_vllm_probe import ManagedVllmProbeError, probe_managed_vllm  # noqa: E402


async def check():
    profile = ProviderProfile("test/probe", "test/model", Availability.AVAILABLE, qualification_id="a" * 64)
    binding = ManagedVllmBinding(profile, "test/model", "http://127.0.0.1:8000", "secret", (0, 1), 2, 1)
    seen = []

    def response(request):
        seen.append(request.url.path)
        assert request.headers["authorization"] == "Bearer secret"
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "0.30.0"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "test/model", "max_model_len": 2048}]})
        raise AssertionError(request.url.path)

    result = await probe_managed_vllm(binding, expected_max_model_len=2048, transport=httpx.MockTransport(response))
    assert (result.model_id, result.backend_version, result.max_model_len) == ("test/model", "0.30.0", 2048)
    assert seen == ["/health", "/version", "/v1/models"]

    async def reject(mutator, label):
        try:
            await probe_managed_vllm(
                binding, expected_max_model_len=2048, transport=httpx.MockTransport(mutator), seconds=0.3
            )
        except ManagedVllmProbeError:
            return
        raise AssertionError(label)

    await reject(lambda request: httpx.Response(503) if request.url.path == "/health" else response(request), "health")
    await reject(
        lambda request: httpx.Response(200, json={"version": "0.29.0"})
        if request.url.path == "/version"
        else response(request),
        "version",
    )
    await reject(
        lambda request: httpx.Response(200, json={"object": "list", "data": [{"id": "wrong", "max_model_len": 2048}]})
        if request.url.path == "/v1/models"
        else response(request),
        "model",
    )
    await reject(
        lambda request: httpx.Response(
            200, json={"object": "list", "data": [{"id": "test/model", "max_model_len": 4096}]}
        )
        if request.url.path == "/v1/models"
        else response(request),
        "context",
    )
    await reject(
        lambda request: httpx.Response(
            200, json={"object": "list", "data": [{"id": "test/model", "max_model_len": True}]}
        )
        if request.url.path == "/v1/models"
        else response(request),
        "boolean context",
    )
    await reject(
        lambda request: httpx.Response(
            200, json={"object": "list", "data": [{"id": "test/model", "max_model_len": 2048}] * 2}
        )
        if request.url.path == "/v1/models"
        else response(request),
        "extra model",
    )
    await reject(
        lambda request: httpx.Response(302, headers={"location": "http://elsewhere/health"})
        if request.url.path == "/health"
        else response(request),
        "redirect",
    )
    await reject(
        lambda request: httpx.Response(200, content=b"x" * 8193)
        if request.url.path == "/health"
        else response(request),
        "oversize",
    )

    async def slow(_request):
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    try:
        await probe_managed_vllm(
            binding, expected_max_model_len=2048, transport=httpx.MockTransport(slow), seconds=0.01
        )
    except ManagedVllmProbeError:
        pass
    else:
        raise AssertionError("expired probe admitted")
    unavailable = ManagedVllmBinding(
        ProviderProfile("test/off", "test/model", Availability.UNAVAILABLE),
        "test/model",
        "http://127.0.0.1:8000",
        "secret",
        (0,),
        1,
        1,
    )
    try:
        await probe_managed_vllm(unavailable, expected_max_model_len=2048)
    except ValueError:
        pass
    else:
        raise AssertionError("unavailable provider probed")
    print("managed vLLM runtime probe PASS")


if __name__ == "__main__":
    asyncio.run(check())
