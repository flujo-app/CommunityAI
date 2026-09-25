"""Focused noncash funding -> API inference -> usage settlement/refund check.

Fake backend and temporary SQLite only; no model, GPU, processor or full CI run.
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from drift.api.server import create_app
from drift.commerce_simulator import CommerceSimulator, SimulatedServiceQuote
from drift.inference_provider import Availability, ProviderIdentity, ProviderProfile
from drift.managed_llama_cpp import ManagedLlamaCppAdapter, ManagedLlamaCppBinding
from drift.managed_vllm_text import ManagedProviderUnavailable, ManagedVllmTextClient
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.simulated_paid_text import SimulatedPaidTextClient
from drift.text_request import RequestContext

MODEL = "test/paid-qwen"
DIGEST = "sha256:" + "c" * 64
PROFILE = ProviderProfile("test/paid-profile", MODEL, Availability.AVAILABLE, qualification_id="a" * 64)
BINDING = ManagedLlamaCppBinding(PROFILE, MODEL, "http://127.0.0.1:18247", "backend-key", (0,), "none", 256)


def quote_for_request(body, chat, context):
    assert chat is False and body["model"] == DIGEST
    return SimulatedServiceQuote(
        request_id=context.request_id, buyer_id=context.caller_id, provider_id="provider",
        funding_source="purchased", model_id=MODEL, profile_id=PROFILE.profile_id,
        service_class="text_inference", settlement_domain="local_simulation",
        artifact_sha256=DIGEST[7:], service_policy_sha256="b" * 64,
        price_schedule_sha256="d" * 64, input_unit_price=1, output_unit_price=1,
        max_input_units=20, max_output_units=4, fee_bps=1000,
        spend_cap=24, expires_at_unix=int(time.time()) + 60,
    )


def fake_response(request):
    payload = json.loads(request.content)
    model = "wrong/model" if payload["prompt"] == "bad" else MODEL
    frames = [
        {"id": "fake-1", "model": model, "choices": [{"index": 0, "text": "hello", "finish_reason": None}]},
        {"id": "fake-1", "model": model, "choices": [{"index": 0, "text": "", "finish_reason": "length"}]},
        {"id": "fake-1", "model": model, "choices": [],
         "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ]
    body = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body + b"data: [DONE]\n\n")


def client_for(journal, backend_client):
    bridge = ManagedVllmTextClient(
        ManagedLlamaCppAdapter(BINDING, client=backend_client),
        ProviderIdentity("provider", "instance"), DIGEST,
    )
    return SimulatedPaidTextClient(bridge, journal, quote_for_request)


async def failed_and_cancelled(journal):
    backend = httpx.AsyncClient(transport=httpx.MockTransport(fake_response))
    try:
        paid = client_for(journal, backend)
        context = RequestContext.start(5, caller_id="buyer")
        try:
            async for _ in paid.stream({"model": DIGEST, "prompt": "bad", "max_tokens": 4},
                                       chat=False, context=context):
                pass
        except ManagedProviderUnavailable:
            pass
        else:
            raise AssertionError("malformed backend identity was paid")
        assert journal.buyer_wallet("buyer")["purchased_available"] == 97
    finally:
        await backend.aclose()

    backend = httpx.AsyncClient(transport=httpx.MockTransport(fake_response))
    try:
        paid = client_for(journal, backend)
        context = RequestContext.start(5, caller_id="buyer")
        iterator = paid.stream({"model": DIGEST, "prompt": "cancel", "max_tokens": 4},
                               chat=False, context=context)
        assert (await anext(iterator))["type"] == "heartbeat"
        assert journal.buyer_wallet("buyer")["service_held"] == 24
        await iterator.aclose()
        assert journal.buyer_wallet("buyer")["service_held"] == 0
        assert journal.buyer_wallet("buyer")["purchased_available"] == 97
    finally:
        await backend.aclose()


def main():
    with tempfile.TemporaryDirectory(prefix="communityai-paid-test-") as temporary:
        with CommerceSimulator(Path(temporary) / "journal.db") as journal:
            assert journal.create_order("order", "buyer", "processor", 100)
            assert journal.record_verified_processor_event("capture", "processor", "capture", 100)
            backend = httpx.AsyncClient(transport=httpx.MockTransport(fake_response))
            try:
                manager = ModelManager()
                paid = client_for(journal, backend)
                manager.register(ModelDescriptor(MODEL, manifest_digest=DIGEST),
                                 lambda: ModelRuntime(model=None, tokenizer=None, text_client=paid))
                app = create_app(
                    model_manager=manager,
                    api_key_identifier=lambda key: "buyer" if key == "buyer-key" else None,
                    request_timeout=5.0,
                )
                with TestClient(app) as api:
                    request = {"model": MODEL, "prompt": "hi", "max_tokens": 4, "stream": False}
                    assert api.post("/v1/completions", json=request).status_code == 401
                    headers = {"Authorization": "Bearer buyer-key"}
                    too_large = api.post("/v1/completions", json={**request, "max_tokens": 5}, headers=headers)
                    assert too_large.status_code == 400 and journal.buyer_wallet("buyer")["service_held"] == 0
                    answer = api.post("/v1/completions", json=request, headers=headers)
                    assert answer.status_code == 200, answer.text
                    assert answer.json()["usage"]["total_tokens"] == 3
                assert journal.buyer_wallet("buyer")["purchased_available"] == 97
                assert journal.provider_wallet("provider")["pending"] == 3
                assert journal.buyer_wallet("buyer")["service_held"] == 0
            finally:
                asyncio.run(backend.aclose())
            asyncio.run(failed_and_cancelled(journal))
            assert journal.audit()["unfunded_reversal_loss"] == 0
    print("PASS: simulated paid API success, no-charge rejection, failure and cancellation refunds")


if __name__ == "__main__":
    main()
