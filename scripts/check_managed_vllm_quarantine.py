"""Focused stop-uncertainty quarantine checks with no vLLM or GPU."""

# isort: skip_file

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import httpx

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import (
    Availability,
    EventKind,
    InferenceLimits,
    InferenceRequest,
    ProviderIdentity,
    ProviderProfile,
)  # noqa: E402
from drift.managed_vllm import ManagedVllmAdapter, ManagedVllmBinding, ManagedVllmStopUnconfirmed  # noqa: E402


def fixture():
    profile = ProviderProfile("test/quarantine", "test/model", Availability.AVAILABLE, qualification_id="a" * 64)
    binding = ManagedVllmBinding(profile, "test/model", "http://127.0.0.1:8000", "secret", (0,), 1, 1)
    now = time.monotonic()
    request = InferenceRequest(
        ProviderIdentity("test/provider", "test/instance"),
        profile.profile_id,
        profile.model_id,
        "a" * 32,
        "b" * 32,
        now,
        now + 2,
        "hello",
        InferenceLimits(100, 20, 8, 100),
    )
    return binding, request


def body(model="test/model"):
    frames = [
        {"id": "response-1", "model": model, "choices": [{"index": 0, "text": "hi", "finish_reason": None}]},
        {"id": "response-1", "model": model, "choices": [{"index": 0, "text": "", "finish_reason": "stop"}]},
        {
            "id": "response-1",
            "model": model,
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    return b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames) + b"data: [DONE]\n\n"


class Slow(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.05)
        yield body()


async def check():
    binding, request = fixture()
    calls = []

    def good(http_request):
        calls.append(http_request)
        return httpx.Response(200, content=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(good)) as client:
        adapter = ManagedVllmAdapter(binding, client=client)
        first = adapter.stream(request)
        assert (await anext(first)).kind is EventKind.STARTED
        busy = [event async for event in adapter.stream(request)]
        assert len(busy) == 1 and busy[0].failure_code == "backend_busy" and len(calls) == 1
        rest = [event async for event in first]
        assert rest[-1].kind is EventKind.COMPLETED and not adapter.quarantined
        repeat = [event async for event in adapter.stream(request)]
        assert repeat[-1].kind is EventKind.COMPLETED and len(calls) == 2

        interrupted = adapter.stream(request)
        assert (await anext(interrupted)).kind is EventKind.STARTED
        await interrupted.aclose()
        assert adapter.quarantined
        denied = [event async for event in adapter.stream(request)]
        assert len(denied) == 1 and denied[0].failure_code == "backend_quarantined" and len(calls) == 3

    wrong = httpx.MockTransport(lambda _: httpx.Response(200, content=body("wrong")))
    async with httpx.AsyncClient(transport=wrong) as client:
        adapter = ManagedVllmAdapter(binding, client=client)
        failed = [event async for event in adapter.stream(request)]
        assert failed[-1].kind is EventKind.FAILED and adapter.quarantined

    slow = httpx.MockTransport(lambda _: httpx.Response(200, stream=Slow()))
    async with httpx.AsyncClient(transport=slow) as client:
        adapter = ManagedVllmAdapter(binding, client=client)
        now = time.monotonic()
        short = InferenceRequest(
            request.identity,
            request.profile_id,
            request.model_id,
            request.request_id,
            request.attempt_id,
            now,
            now + 0.01,
            request.prompt,
            request.limits,
        )
        try:
            [event async for event in adapter.stream(short)]
        except ManagedVllmStopUnconfirmed:
            assert adapter.quarantined
        else:
            raise AssertionError("unconfirmed backend stop accepted")
    print("managed vLLM quarantine PASS")


if __name__ == "__main__":
    asyncio.run(check())
