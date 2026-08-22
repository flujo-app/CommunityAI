"""Compatibility gate against the official OpenAI Python client over a real TCP socket."""

import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import torch

pytest.importorskip("fastapi")
openai = pytest.importorskip("openai")
uvicorn = pytest.importorskip("uvicorn")

from drift.api.server import create_app


class _Tokenizer:
    eos_token_id = 0

    def apply_chat_template(self, messages, **kwargs):
        return {"input_ids": torch.tensor([[1, 2]])}

    def __call__(self, text, return_tensors=None):
        return SimpleNamespace(input_ids=torch.tensor([[1, 2]]))

    def decode(self, ids, **kwargs):
        return " ".join(f"tok{token}" for token in torch.as_tensor(ids).flatten().tolist())


class _Model:
    def generate(self, input_ids, streamer=None, max_new_tokens=None, **kwargs):
        tokens = [101, 102, 103][:max_new_tokens]
        outputs = torch.cat([input_ids, torch.tensor([tokens])], dim=1)
        if streamer is not None:
            streamer.put(input_ids)
            for token in tokens:
                streamer.put(torch.tensor([[token]]))
            streamer.end()
        return outputs


@contextmanager
def _live_server(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise RuntimeError("the localhost compatibility server did not start")
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "the localhost compatibility server did not stop"


def test_official_python_client_models_completions_chat_and_streaming():
    app = create_app(_Model(), _Tokenizer(), model_name="local-test", api_keys=["local-key"])
    with _live_server(app) as base_url:
        client = openai.OpenAI(api_key="local-key", base_url=base_url, max_retries=0, timeout=5)

        models = client.models.list()
        completion = client.completions.create(model="local-test", prompt="hello", max_tokens=2, temperature=0)
        chat = client.chat.completions.create(
            model="local-test", messages=[{"role": "user", "content": "hello"}], max_tokens=3, temperature=0
        )
        stream = client.chat.completions.create(
            model="local-test",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=3,
            temperature=0,
            stream=True,
        )
        streamed = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)

        assert [model.id for model in models.data] == ["local-test"]
        assert completion.model == "local-test"
        assert completion.choices[0].text == "tok101 tok102"
        assert chat.model == "local-test"
        assert chat.choices[0].message.content == "tok101 tok102 tok103"
        assert streamed.replace(" ", "") == "tok101tok102tok103"


def test_official_python_client_observes_bearer_authentication():
    app = create_app(_Model(), _Tokenizer(), model_name="local-test", api_keys=["local-key"])
    with _live_server(app) as base_url:
        client = openai.OpenAI(api_key="wrong-key", base_url=base_url, max_retries=0, timeout=5)
        with pytest.raises(openai.AuthenticationError):
            client.models.list()
