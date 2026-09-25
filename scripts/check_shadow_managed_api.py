"""Standalone, offline FastAPI -> managed vLLM -> shadow ledger check.

Uses an in-process fake backend and temporary SQLite/key files. No GPU,
vLLM installation, real model, network download, or CI run is required.
"""

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from drift.api.server import create_app
from drift.inference_provider import Availability, ProviderIdentity, ProviderProfile
from drift.managed_vllm import ManagedVllmAdapter, ManagedVllmBinding
from drift.managed_vllm_text import ManagedVllmTextClient
from drift.node.keys import ApiKeyStore
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.shadow_credits import ShadowLedger
from drift.shadow_metered_managed import ShadowMeteredManagedClient


MANIFEST = "sha256:" + "c" * 64
CALLS = []


class FakeBackend(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
        CALLS.append(payload)
        if payload["prompt"] == "fail":
            body = b"data: [DONE]\n\n"
        else:
            frames = [
                {"id": "local-1", "model": payload["model"], "choices": [
                    {"index": 0, "text": "ok", "finish_reason": None}]},
                {"id": "local-1", "model": payload["model"], "choices": [
                    {"index": 0, "text": "", "finish_reason": "stop"}]},
                {"id": "local-1", "model": payload["model"], "choices": [], "usage": {
                    "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            body = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
            body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_store = ApiKeyStore(root / "api-keys.json")
            metadata, secret = key_store.create(label="shadow test buyer")
            other_metadata, other_secret = key_store.create(label="unfunded test buyer")
            ledger_path = root / "shadow.db"
            with ShadowLedger(ledger_path) as ledger:
                ledger.grant_test_credits("fixture_grant", metadata["id"], 4096)
                profile = ProviderProfile("test/metered", "test/model", Availability.AVAILABLE,
                                          qualification_id="e" * 64)
                binding = ManagedVllmBinding(
                    profile, "test/model", f"http://127.0.0.1:{server.server_port}",
                    "local-only", (0,), 1, 1,
                )
                bridge = ManagedVllmTextClient(
                    ManagedVllmAdapter(binding), ProviderIdentity("test/provider", "test/instance"), MANIFEST
                )
                metered = ShadowMeteredManagedClient(bridge, ledger)
                manager = ModelManager()
                manager.register(
                    ModelDescriptor("test/model", manifest_digest=MANIFEST),
                    lambda: ModelRuntime(model=None, tokenizer=None, text_client=metered),
                )
                app = create_app(model_manager=manager, api_key_identifier=key_store.identify,
                                 request_timeout=5.0)
                with TestClient(app) as client:
                    request = {"model": "test/model", "prompt": "hello", "max_tokens": 20}
                    assert client.post("/v1/completions", json=request).status_code == 401
                    headers = {"Authorization": "Bearer " + secret}
                    good = client.post("/v1/completions", json=request, headers=headers)
                    assert good.status_code == 200, good.text
                    assert good.json()["usage"]["total_tokens"] == 2
                    assert len(CALLS) == 1
                    wallet = ledger.buyer_wallet(metadata["id"])
                    assert (wallet["available"], wallet["held"], wallet["pending_receipts"]) == (2048, 2048, 1)
                    receipt = ledger.pending_receipts()[0]
                    assert receipt.input_units == receipt.output_units == 1
                    assert receipt.proposed_charge == 2 and receipt.provider_id == metered.provider_id
                    unfunded = client.post("/v1/completions", json=request,
                                           headers={"Authorization": "Bearer " + other_secret})
                    assert unfunded.status_code == 400 and len(CALLS) == 1
                    streamed = client.post("/v1/completions", json={**request, "stream": True}, headers=headers)
                    assert streamed.status_code == 200 and "data: [DONE]" in streamed.text
                    assert len(CALLS) == 2 and len(ledger.pending_receipts()) == 2, (streamed.text, len(CALLS))
                    no_balance = client.post("/v1/completions", json=request, headers=headers)
                    assert no_balance.status_code == 400 and len(CALLS) == 2
                    ledger.grant_test_credits("failure_fixture", other_metadata["id"], 2048)
                    failed = client.post("/v1/completions", json={**request, "prompt": "fail"},
                                         headers={"Authorization": "Bearer " + other_secret})
                    assert failed.status_code == 503, failed.text
                    assert len(CALLS) == 3
                    other_wallet = ledger.buyer_wallet(other_metadata["id"])
                    assert (other_wallet["available"], other_wallet["held"], other_wallet["pending_receipts"]) == (2048, 0, 0)
                    key_store.revoke(metadata["id"])
                    assert client.post("/v1/completions", json=request, headers=headers).status_code == 401
                    assert len(CALLS) == 3
                    assert key_store.identify(other_secret) == other_metadata["id"]
                assert all(item.active_requests == 0 for item in manager.snapshots())
                assert ledger.audit()["receipts"] == 2
            assert b"hello" not in ledger_path.read_bytes()
            assert b"local-only" not in ledger_path.read_bytes()
            with ShadowLedger(ledger_path) as reopened:
                pending = reopened.pending_receipts()
                assert len(pending) == 2 and pending[0] == receipt
                assert reopened.buyer_wallet(metadata["id"])["held"] == 4096
                for claim in pending:
                    reopened.reject_receipt(claim.receipt_id, "fixture_rejected")
                    reopened.finalize(claim.request_id)
                assert reopened.buyer_wallet(metadata["id"])["available"] == 4096
                reopened.audit()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print("shadow managed API check: auth, held quote, pending receipt, failure refund, restart PASS")


if __name__ == "__main__":
    main()
