"""
Offline tests for the OpenAI-compatible API shim (`drift api`, src/drift/api/server.py): request
mapping, non-streaming and SSE responses, auth. No swarm, no network -- the model and tokenizer
are minimal fakes; the streaming path still runs through the real transformers TextIteratorStreamer.
"""

import json
from types import SimpleNamespace

import pytest
import torch

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from drift.api.server import build_generate_kwargs, create_app, message_text, trim_stop_strings

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
