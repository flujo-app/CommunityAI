"""
Offline tests for the OpenAI-compatible API shim (`drift api`, src/drift/api/server.py): request
mapping, non-streaming and SSE responses, auth. No swarm, no network -- the model and tokenizer
are minimal fakes; the streaming path still runs through the real transformers TextIteratorStreamer.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
import torch

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from drift.api.server import build_generate_kwargs, create_app, message_text, trim_stop_strings
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime, ModelState

NEW_TOKENS = [101, 102, 103]


class FakeTokenizer:
    eos_token_id = 0

    def apply_chat_template(self, messages, add_generation_prompt=True, return_dict=False, return_tensors=None):
        n_tokens = sum(len(str(m["content"]).split()) for m in messages) + 2
        return {"input_ids": torch.arange(1, n_tokens + 1).unsqueeze(0)}

    def __call__(self, text, return_tensors=None):
        return SimpleNamespace(input_ids=torch.arange(1, len(text.split()) + 1).unsqueeze(0))

    def decode(self, ids, **kwargs):
        ids = torch.as_tensor(ids).flatten().tolist()
        return " ".join(f"tok{int(i)}" for i in ids)


class FakeModel:
    """Mimics model.generate(): appends NEW_TOKENS and, when streaming, drives the streamer."""

    def __init__(self):
        self.last_gen_kwargs = None

    def generate(self, input_ids, tokenizer=None, streamer=None, max_new_tokens=None, **kwargs):
        self.last_gen_kwargs = dict(kwargs, max_new_tokens=max_new_tokens)
        if tokenizer is not None:
            self.last_gen_kwargs["tokenizer"] = tokenizer
        new_tokens = NEW_TOKENS[: max_new_tokens if max_new_tokens is not None else len(NEW_TOKENS)]
        output_ids = torch.cat([input_ids, torch.tensor([new_tokens])], dim=1)
        if streamer is not None:
            streamer.put(input_ids)  # the prompt, skipped via skip_prompt=True
            for token in new_tokens:
                streamer.put(torch.tensor([[token]]))
            streamer.end()
        return output_ids


@pytest.fixture
def api():
    model = FakeModel()
    app = create_app(model, FakeTokenizer(), model_name="fake/model")
    return SimpleNamespace(client=TestClient(app), model=model)


def test_build_generate_kwargs_maps_openai_semantics():
    greedy = build_generate_kwargs(max_tokens=None, temperature=0.0, top_p=None, stop=None, default_max_tokens=64)
    assert greedy == {"max_new_tokens": 64, "do_sample": False}

    sampled = build_generate_kwargs(max_tokens=10, temperature=0.7, top_p=0.9, stop="END", default_max_tokens=64)
    assert sampled == {
        "max_new_tokens": 10,
        "do_sample": True,
        "temperature": 0.7,
        "top_p": 0.9,
        "stop_strings": ["END"],
    }

    # OpenAI default (no temperature given) is sampling
    assert build_generate_kwargs(max_tokens=None, temperature=None, top_p=None, stop=None)["do_sample"] is True


def test_message_text_flattens_content_parts():
    assert message_text("plain") == "plain"
    assert message_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"


def test_trim_stop_strings():
    assert trim_stop_strings("hello END world", "END") == "hello "
    assert trim_stop_strings("hello", ["END"]) == "hello"


def test_models_and_health(api):
    assert api.client.get("/health").json()["status"] == "ok"
    models = api.client.get("/v1/models").json()
    assert models["data"][0]["id"] == "fake/model"


def test_chat_completion_non_stream(api):
    response = api.client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi there"}], "temperature": 0, "max_tokens": 8},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "tok101 tok102 tok103"
    assert body["choices"][0]["finish_reason"] == "stop"  # 3 tokens < max_tokens=8
    assert body["usage"]["completion_tokens"] == len(NEW_TOKENS)
    assert api.model.last_gen_kwargs["do_sample"] is False


def test_requested_model_must_resolve_exactly(api):
    response = api.client.post(
        "/v1/chat/completions",
        json={"model": "different/model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404
    assert "unknown model" in response.json()["detail"]


def test_tokenizer_generate_kwarg_is_only_sent_for_stop_strings(api):
    plain = api.client.post("/v1/completions", json={"model": "fake/model", "prompt": "hi", "temperature": 0})
    assert plain.status_code == 200
    assert "tokenizer" not in api.model.last_gen_kwargs

    stopped = api.client.post(
        "/v1/completions",
        json={"model": "fake/model", "prompt": "hi", "temperature": 0, "stop": "tok102"},
    )
    assert stopped.status_code == 200
    assert api.model.last_gen_kwargs["tokenizer"].__class__ is FakeTokenizer


def test_multi_model_requests_require_and_select_a_registered_model():
    first, second = FakeModel(), FakeModel()
    manager = ModelManager()
    manager.register_loaded("first", first, FakeTokenizer())
    manager.register_loaded("second", second, FakeTokenizer(), aliases=("second-alias",))
    client = TestClient(create_app(model_manager=manager))

    missing = client.post("/v1/completions", json={"prompt": "hi"})
    assert missing.status_code == 400

    selected = client.post("/v1/completions", json={"model": "second-alias", "prompt": "hi"})
    assert selected.status_code == 200
    assert selected.json()["model"] == "second"
    assert first.last_gen_kwargs is None
    assert second.last_gen_kwargs is not None


def test_chat_completion_finish_reason_length(api):
    response = api.client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2},
    )
    assert response.json()["choices"][0]["finish_reason"] == "length"


def test_chat_completion_stream(api):
    with api.client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True, "max_tokens": 8},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[len("data: ") :]) for line in lines[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    streamed_text = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    assert streamed_text.replace(" ", "") == "tok101tok102tok103"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_completions_endpoint(api):
    response = api.client.post("/v1/completions", json={"prompt": "one two three", "temperature": 0})
    body = response.json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "tok101 tok102 tok103"
    assert body["usage"]["prompt_tokens"] == 3


def test_api_key_auth():
    app = create_app(FakeModel(), FakeTokenizer(), model_name="fake/model", api_keys=["sekrit"])
    client = TestClient(app)
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer sekrit"}).status_code == 200


def test_rejects_multiple_choices(api):
    response = api.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "n": 2})
    assert response.status_code == 400


def test_api_cli_rejects_revocations_outside_manifest_mode(monkeypatch):
    from drift.cli import run_api

    monkeypatch.setattr(
        "sys.argv",
        [
            "drift api",
            "org/model",
            "--initial_peers",
            "/ip4/127.0.0.1/tcp/1/p2p/fake",
            "--torch_dtype",
            "float32",
            "--revocation_file",
            "revoked.json",
        ],
    )
    with pytest.raises(SystemExit):
        run_api.main()


@pytest.mark.asyncio
async def test_cancelled_lazy_load_releases_its_eventual_runtime_lease():
    loader_started = threading.Event()
    allow_loader_to_finish = threading.Event()
    manager = ModelManager(max_loaded_models=1)

    def loader():
        loader_started.set()
        assert allow_loader_to_finish.wait(timeout=2)
        return ModelRuntime(FakeModel(), FakeTokenizer())

    manager.register(ModelDescriptor("model"), loader)
    app = create_app(model_manager=manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = asyncio.create_task(client.post("/v1/completions", json={"model": "model", "prompt": "hi"}))
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, loader_started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        allow_loader_to_finish.set()

        for _ in range(100):
            snapshot = manager.snapshots()[0]
            if snapshot.state is ModelState.READY and snapshot.active_requests == 0:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cancelled load left a runtime lease active")

    manager.shutdown()


@pytest.mark.asyncio
async def test_cancelled_generation_keeps_lease_until_executor_finishes():
    generation_started = threading.Event()
    allow_generation_to_finish = threading.Event()

    class BlockingModel(FakeModel):
        def generate(self, input_ids, **kwargs):
            generation_started.set()
            assert allow_generation_to_finish.wait(timeout=2)
            return torch.cat([input_ids, torch.tensor([[101]])], dim=1)

    manager = ModelManager(max_loaded_models=1)
    manager.register_loaded("model", BlockingModel(), FakeTokenizer())
    app = create_app(model_manager=manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = asyncio.create_task(client.post("/v1/completions", json={"model": "model", "prompt": "hi"}))
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, generation_started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        assert manager.snapshots()[0].active_requests == 1
        allow_generation_to_finish.set()
        for _ in range(100):
            if manager.snapshots()[0].active_requests == 0:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cancelled generation released its runtime too early or stranded the lease")

    manager.shutdown()
