"""Bounded local owner check against the already cached pinned CUDA/GGUF assets.

Runs one server for at most 90 seconds of startup plus two short requests.
No CI suite, downloads, remote worker, or public route is involved.
"""

# isort: skip_file

import asyncio
import hashlib
import socket
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

from drift.inference_provider import (  # noqa: E402
    Availability, EventKind, InferenceLimits, InferenceRequest, ProviderIdentity, ProviderProfile,
)
from drift.managed_llama_cpp import ManagedLlamaCppBinding  # noqa: E402
from drift.managed_llama_cpp_process import (  # noqa: E402
    ManagedLlamaCppProcessError, ManagedLlamaCppProcessOwner,
)
from drift.commerce_simulator import CommerceSimulator, SimulatedServiceQuote  # noqa: E402
from drift.managed_vllm_text import ManagedVllmTextClient  # noqa: E402
from drift.simulated_paid_text import SimulatedPaidTextClient  # noqa: E402
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime  # noqa: E402
from drift.api.server import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / ".gate13-runs" / "llama-b11173"
SERVER = ROOT / "run" / "llama-server.exe"
MODEL = ROOT / "qwen3-17b-f16.gguf"
SERVER_SHA = "925ea12e72149baba11041f77394b6e07951eac6693118762db882b78687172e"
MODEL_SHA = "11612b8bbfbbe59d484541a6bcda2a8d84632600fdfbe00d2290c726bb6cbf7d"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(profile: ProviderProfile, prompt: str) -> InferenceRequest:
    now = time.monotonic()
    return InferenceRequest(
        ProviderIdentity("test/local-provider", "test/local-instance"),
        profile.profile_id, profile.model_id, "a" * 32, "b" * 32,
        now, now + 20, prompt, InferenceLimits(252, 4, 100, 4096),
    )


async def main() -> None:
    profile = ProviderProfile(
        "test/qwen-llama-owned", "test/qwen3-1.7b-llama-owned",
        Availability.AVAILABLE, qualification_id="1" * 64,
    )
    binding = ManagedLlamaCppBinding(
        profile, profile.model_id, f"http://127.0.0.1:{free_port()}",
        "local-owner-secret", (0,), "none", 256,
    )
    arguments = dict(
        executable=SERVER, executable_sha256=SERVER_SHA,
        model_file=MODEL, model_sha256=MODEL_SHA,
    )
    owner = ManagedLlamaCppProcessOwner(binding, **arguments)
    bad_owner = ManagedLlamaCppProcessOwner(binding, **{**arguments, "model_sha256": "0" * 64})
    try:
        bad_owner.start()
    except ManagedLlamaCppProcessError:
        assert bad_owner.pid is None
    else:
        raise AssertionError("unverified GGUF was launched")
    try:
        owner.start()
        deadline = time.monotonic() + 90
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                assert owner.running, "owned llama.cpp server exited before readiness"
                try:
                    response = await client.get(binding.base_url + "/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
            else:
                raise TimeoutError("owned llama.cpp startup exceeded 90 seconds")
        probe = await owner.probe(seconds=5)
        assert probe.build_info == "b11173-84e76d8a2" and owner.ready
        adapter = owner.new_adapter()
        events = [event async for event in adapter.stream(request(profile, "Reply with one short greeting."))]
        assert events[-1].kind is EventKind.COMPLETED and events[-1].usage.output_units > 0
        assert owner.ready
        digest = "sha256:" + MODEL_SHA
        with tempfile.TemporaryDirectory(prefix="communityai-llama-paid-") as temporary:
            with CommerceSimulator(Path(temporary) / "journal.db") as journal:
                journal.create_order("order", "buyer", "processor", 100)
                journal.record_verified_processor_event("capture", "processor", "capture", 100)

                def quote_for_request(_body, _chat, context):
                    return SimulatedServiceQuote(
                        request_id=context.request_id, buyer_id=context.caller_id,
                        provider_id="provider", funding_source="purchased",
                        model_id=profile.model_id, profile_id=profile.profile_id,
                        service_class="text_inference", settlement_domain="local_simulation",
                        artifact_sha256=MODEL_SHA, service_policy_sha256="b" * 64,
                        price_schedule_sha256="c" * 64, input_unit_price=1,
                        output_unit_price=1, max_input_units=16, max_output_units=4,
                        fee_bps=1000, spend_cap=20, expires_at_unix=int(time.time()) + 60,
                    )

                bridge = ManagedVllmTextClient(
                    owner.new_adapter(), ProviderIdentity("provider", "instance"), digest,
                )
                paid = SimulatedPaidTextClient(bridge, journal, quote_for_request)
                manager = ModelManager()
                manager.register(
                    ModelDescriptor(profile.model_id, manifest_digest=digest),
                    lambda: ModelRuntime(model=None, tokenizer=None, text_client=paid),
                )
                app = create_app(
                    model_manager=manager,
                    api_key_identifier=lambda key: "buyer" if key == "buyer-key" else None,
                    request_timeout=20.0,
                )
                with TestClient(app) as client:
                    answer = client.post(
                        "/v1/completions",
                        json={"model": profile.model_id, "prompt": "Reply with one short greeting.",
                              "max_tokens": 4, "temperature": 0, "stream": False},
                        headers={"Authorization": "Bearer buyer-key"},
                    )
                    assert answer.status_code == 200, answer.text
                    usage = answer.json()["usage"]
                    charge = usage["prompt_tokens"] + usage["completion_tokens"]
                    assert charge > 0
                assert journal.buyer_wallet("buyer")["purchased_available"] == 100 - charge
                assert journal.buyer_wallet("buyer")["service_held"] == 0
                assert journal.provider_wallet("provider")["pending"] == charge - charge // 10
                assert journal.audit()["unfunded_reversal_loss"] == 0
        iterator = adapter.stream(request(profile, "Continue briefly."))
        assert (await anext(iterator)).kind is EventKind.STARTED
        await iterator.aclose()
        assert adapter.quarantined and owner.pid is None and not owner.ready
        try:
            owner.new_adapter()
        except ManagedLlamaCppProcessError:
            pass
        else:
            raise AssertionError("stopped llama.cpp owner admitted a route")
        print("PASS: pinned llama.cpp launch, owned metadata, simulated paid API and cancellation teardown")
    finally:
        owner.stop()


if __name__ == "__main__":
    asyncio.run(main())
