"""Fast local vLLM adapter checks using httpx MockTransport, no vLLM/GPU."""

import asyncio
import json
import sys
import tempfile
import time
import types
from dataclasses import replace
from pathlib import Path

import httpx


PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift import inference_provider as contract  # noqa: E402
from drift.managed_vllm import ManagedVllmAdapter, ManagedVllmBinding, ManagedVllmStopUnconfirmed  # noqa: E402


class Chunked(httpx.AsyncByteStream):
    async def __aiter__(self):
        body = sse()
        for index in range(0, len(body), 17):
            yield body[index : index + 17]


class Slow(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.05)
        yield sse()


def fixture(*, available=True):
    profile = contract.ProviderProfile(
        "test/profile",
        "test/model",
        contract.Availability.AVAILABLE if available else contract.Availability.UNAVAILABLE,
        qualification_id="e" * 64 if available else None,
    )
    binding = ManagedVllmBinding(profile, "test/model", "http://127.0.0.1:8000", "secret", (0, 1), 2, 1)
    now = time.monotonic()
    request = contract.InferenceRequest(
        contract.ProviderIdentity("test/provider", "test/instance"),
        profile.profile_id, profile.model_id, "a" * 32, "b" * 32,
        now, now + 5.0, "hello", contract.InferenceLimits(100, 20, 8, 100),
    )
    return binding, request


def sse(model="test/model", *, done=True):
    frames = [
        {"id": "req1", "model": model, "choices": [{"index": 0, "text": "hi", "finish_reason": None}]},
        {"id": "req1", "model": model, "choices": [{"index": 0, "text": "", "finish_reason": "stop"}]},
        {
            "id": "req1", "model": model, "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    body = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
    return body + (b"data: [DONE]\n\n" if done else b"")


async def check():
    binding, request = fixture()
    calls = []

    def respond(http_request):
        calls.append(http_request)
        assert http_request.url.path == "/v1/completions"
        assert http_request.headers["Authorization"] == "Bearer secret"
        payload = json.loads(http_request.content)
        assert payload["model"] == "test/model" and payload["prompt"] == "hello"
        assert payload["stream_options"] == {"include_usage": True}
        return httpx.Response(200, stream=Chunked(), headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        events = [event async for event in ManagedVllmAdapter(binding, client=client).stream(request)]
    assert [event.kind for event in events] == [
        contract.EventKind.STARTED, contract.EventKind.OUTPUT, contract.EventKind.COMPLETED,
    ]
    assert events[-1].usage == contract.Usage(1, 1, 2) and len(calls) == 1
    with tempfile.TemporaryDirectory() as model_directory:
        command, environment = binding.launch_spec(Path(model_directory), max_model_len=2048)
        assert ("--tensor-parallel-size", "2") == command[command.index("--tensor-parallel-size") :][:2]
        assert ("--pipeline-parallel-size", "1") == command[command.index("--pipeline-parallel-size") :][:2]
        assert environment == {"CUDA_VISIBLE_DEVICES": "0,1", "VLLM_API_KEY": "secret"}
        assert "secret" not in command and "--no-enable-log-requests" in command
        eight = ManagedVllmBinding(
            binding.profile, binding.served_model, binding.base_url, "secret", tuple(range(8)), 4, 2
        )
        eight_command, eight_environment = eight.launch_spec(Path(model_directory), max_model_len=2048)
        assert eight_environment["CUDA_VISIBLE_DEVICES"] == "0,1,2,3,4,5,6,7"
        assert eight_command[eight_command.index("--pipeline-parallel-size") + 1] == "2"

    wrong_model = httpx.MockTransport(lambda _: httpx.Response(200, content=sse("wrong")))
    async with httpx.AsyncClient(transport=wrong_model) as client:
        events = [event async for event in ManagedVllmAdapter(binding, client=client).stream(request)]
    assert [event.kind for event in events] == [contract.EventKind.STARTED, contract.EventKind.FAILED]
    assert events[-1].usage is None

    truncated = httpx.MockTransport(lambda _: httpx.Response(200, content=sse(done=False)))
    async with httpx.AsyncClient(transport=truncated) as client:
        events = [event async for event in ManagedVllmAdapter(binding, client=client).stream(request)]
    assert events[-1].kind is contract.EventKind.FAILED and events[-1].usage is None

    now = time.monotonic()
    short_request = replace(request, issued_at=now, deadline=now + 0.01)
    slow = httpx.MockTransport(lambda _: httpx.Response(200, stream=Slow()))
    async with httpx.AsyncClient(transport=slow) as client:
        try:
            [event async for event in ManagedVllmAdapter(binding, client=client).stream(short_request)]
            raise AssertionError("deadline accepted")
        except ManagedVllmStopUnconfirmed:
            pass

    extra = sse() + b'data: {"id":"req1","model":"test/model","choices":[]}\n\n'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=extra))) as client:
        events = [event async for event in ManagedVllmAdapter(binding, client=client).stream(request)]
    assert events[-1].kind is contract.EventKind.FAILED and events[-1].usage is None

    bad_usage = sse().replace(b'"total_tokens": 2', b'"total_tokens": 3')
    wrong_usage = httpx.MockTransport(lambda _: httpx.Response(200, content=bad_usage))
    async with httpx.AsyncClient(transport=wrong_usage) as client:
        events = [event async for event in ManagedVllmAdapter(binding, client=client).stream(request)]
    assert events[-1].kind is contract.EventKind.FAILED and events[-1].usage is None

    unavailable, unavailable_request = fixture(available=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: 1 / 0)) as client:
        events = [event async for event in ManagedVllmAdapter(unavailable, client=client).stream(unavailable_request)]
    assert [event.kind for event in events] == [contract.EventKind.REFUSED]
    assert events[0].refusal.code is contract.RefusalCode.PROFILE_UNAVAILABLE

    for exact in (contract.DEEPSEEK_V41_FLASH, contract.GLM_53):
        required = contract.REQUIRED_PROFILES[exact]
        candidate = ManagedVllmBinding(required, exact, binding.base_url, "secret", (0, 1), 2, 1)
        now = time.monotonic()
        exact_request = contract.InferenceRequest(
            request.identity, required.profile_id, exact, request.request_id, request.attempt_id,
            now, now + 5.0, "test prompt", request.limits,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: 1 / 0)) as client:
            events = [event async for event in ManagedVllmAdapter(candidate, client=client).stream(exact_request)]
        assert [event.kind for event in events] == [contract.EventKind.REFUSED]
        with tempfile.TemporaryDirectory() as model_directory:
            try:
                candidate.launch_spec(Path(model_directory), max_model_len=2048)
                raise AssertionError("unavailable exact model launch accepted")
            except ValueError:
                pass

    try:
        ManagedVllmBinding(binding.profile, "test/model", "http://example.com:8000", "secret", (0, 1), 2, 1)
        raise AssertionError("remote endpoint accepted")
    except ValueError:
        pass
    try:
        ManagedVllmBinding(binding.profile, "test/model", binding.base_url, "secret", (0, 0), 2, 1)
        raise AssertionError("duplicate GPUs accepted")
    except ValueError:
        pass


if __name__ == "__main__":
    asyncio.run(check())
    print("managed vLLM adapter: local stream, identity, usage, refusal and GPU geometry PASS")
