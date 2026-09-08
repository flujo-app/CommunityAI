"""Small canary-driver checks; no model, GUI, GPU, or external route is used."""

import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from hivemind.p2p import P2PHandlerError, PeerID
from hivemind.utils.serializer import MSGPackSerializer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gate16_live_rpc as canary

from drift.model_manifest import ModelManifest
from drift.server.admission import AdmissionPolicy, AdmissionState
from drift.server.handler import TransformerConnectionHandler
from drift.server.health import build_public_worker_health

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT
    / "public-alpha/catalog-qwen-v2/manifests/c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4.json"
)
PEER = PeerID.from_base58("QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo")


def policy_file(tmp_path, **changes):
    source = {"schema_version": 1, "admission": asdict(AdmissionPolicy()), "step_timeout": 30, "session_timeout": 60}
    source.update(changes)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(source))
    return path


@pytest.mark.parametrize(
    "address",
    ["/ip4/127.0.0.1/udp/31337/p2p/" + str(PEER), "/ip4/127.0.0.1/tcp/31337/p2p/" + str(PEER) + "/p2p-circuit"],
)
def test_exact_peer_rejects_non_tcp_and_relay_targets(address):
    with pytest.raises(canary.CanaryError):
        canary.exact_peer(address)


def test_exact_peer_accepts_one_direct_authenticated_target():
    assert canary.exact_peer("/ip4/127.0.0.1/tcp/31337/p2p/" + str(PEER)) == PEER


@pytest.mark.parametrize(
    "changes",
    [
        {"step_timeout": float("nan")},
        {"session_timeout": 61},
        {"step_timeout": True},
        {"step_timeout": 31, "session_timeout": 30},
        {"step_timeout": 5},
        {"admission": asdict(AdmissionPolicy(max_active_sessions=1))},
    ],
)
def test_policy_rejects_unbounded_or_inconsistent_deadlines(tmp_path, changes):
    with pytest.raises(canary.CanaryError):
        canary.load_policy(policy_file(tmp_path, **changes))


def test_payloads_are_bounded_and_cannot_execute_model_tensors():
    manifest = ModelManifest.load(MANIFEST)
    cases = canary.malformed_cases(manifest.dht_prefix + ".0", manifest.digest)
    assert len(cases) == 5
    assert sum(request.ByteSize() for _, request, _ in cases) < canary.MAX_INPUT_BYTES
    assert all(not request.tensors for _, request, _ in cases)
    assert len(cases[0][1].metadata) == canary.MAX_INFERENCE_METADATA_BYTES + 1
    assert MSGPackSerializer.loads(cases[-1][1].metadata)["max_length"] == -1


def test_dry_preflight_never_opens_network(tmp_path, monkeypatch):
    manifest = ModelManifest.load(MANIFEST)
    state = AdmissionState.local(AdmissionPolicy())
    health = tmp_path / "health.json"
    health.write_text(
        json.dumps(
            build_public_worker_health(
                manifest_digest=manifest.digest_id,
                start_block=0,
                end_block=1,
                admission_snapshot=state.snapshot(),
                ready=True,
                announcer_alive=True,
                handlers_alive=True,
                pools_alive=True,
            )
        )
    )

    async def forbidden(**kwargs):
        raise AssertionError("dry preflight must not create a transport")

    monkeypatch.setattr(canary.P2P, "create", forbidden)
    args = SimpleNamespace(
        manifest=MANIFEST,
        expected_manifest_digest=manifest.digest_id,
        block=0,
        worker_multiaddr="/ip4/127.0.0.1/tcp/31337/p2p/" + str(PEER),
        worker_label="owned-worker-a",
        policy=policy_file(tmp_path),
        health=health,
        output=tmp_path / "result",
        execute=False,
    )
    result = asyncio.run(canary.run(args))
    assert result["result"] == "preflight-passed"
    assert result["network_connections"] == 0 and result["executed"] is False
    assert str(PEER) not in json.dumps(result)


def test_health_rejects_stale_or_wrong_manifest(tmp_path):
    manifest = ModelManifest.load(MANIFEST)
    payload = build_public_worker_health(
        manifest_digest=manifest.digest_id,
        start_block=0,
        end_block=1,
        admission_snapshot=AdmissionState.local(AdmissionPolicy()).snapshot(),
        ready=True,
        announcer_alive=True,
        handlers_alive=True,
        pools_alive=True,
        observed_at="2026-01-01T00:00:00Z",
    )
    path = tmp_path / "health.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(canary.CanaryError, match="health_stale"):
        canary.read_health(path, manifest.digest_id, 0)
    with pytest.raises(canary.CanaryError, match="health_manifest_mismatch"):
        canary.read_health(path, "sha256:" + "0" * 64, 0)


def test_child_inspection_failure_preserves_failed_result_json(tmp_path, monkeypatch):
    manifest = ModelManifest.load(MANIFEST)
    snapshots = iter(([], canary.psutil.AccessDenied()))

    def children(**kwargs):
        value = next(snapshots)
        if isinstance(value, Exception):
            raise value
        return value

    async def create(**kwargs):
        return SimpleNamespace()

    async def passed(self):
        return {"checks": [], "rpc_calls": 0, "input_bytes": 0}

    monkeypatch.setattr(canary.psutil, "Process", lambda: SimpleNamespace(children=children))
    monkeypatch.setattr(canary.P2P, "create", create)
    monkeypatch.setattr(canary.TransformerConnectionHandler, "get_stub", lambda *args: None)
    monkeypatch.setattr(canary.Probe, "run", passed)
    monkeypatch.setattr(canary, "read_health", lambda *args: {"admission": {"active_sessions": 0, "pending_pushes": 0}})
    args = SimpleNamespace(
        manifest=MANIFEST,
        expected_manifest_digest=manifest.digest_id,
        block=0,
        worker_multiaddr="/ip4/127.0.0.1/tcp/31337/p2p/" + str(PEER),
        worker_label="owned-worker-a",
        policy=policy_file(tmp_path),
        health=tmp_path / "unused-health.json",
        output=tmp_path / "result",
        execute=True,
    )
    result = asyncio.run(canary.run(args))
    assert result["result"] == "failed" and result["client_cleanup_error_type"] == "AccessDenied"
    assert result["cleanup"] == {"owned_client_stopped": False, "worker_sessions_released": True}
    assert json.loads((args.output / "result.json").read_text())["result"] == "failed"


@pytest.mark.parametrize(
    "real_transport,enforce_peer_cap",
    [(False, True), (True, True), (False, False)],
    ids=["local-handler", "loopback-tls", "missing-peer-cap"],
)
def test_probe_uses_real_handler_admission_and_rejects_without_cache_allocation(real_transport, enforce_peer_cap):
    """The real handler validates every case, including over actual loopback TLS."""
    manifest = ModelManifest.load(MANIFEST)
    policy = AdmissionPolicy(global_session_rate=1000, global_session_burst=100, peer_session_rate=1000)
    state = AdmissionState.local(policy if enforce_peer_cap else replace(policy, max_active_sessions_per_peer=2))
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = state
    handler.step_timeout, handler.session_timeout = 1.0, 3.0
    handler.manifest_digest = manifest.digest
    handler.identity_key_id = "sha256:" + "a" * 64
    handler.inference_max_length = 512
    handler._log_request = lambda *args, **kwargs: None
    handler.dht_prefix = manifest.dht_prefix
    handler.dht = SimpleNamespace(peer_id=PEER, client_mode=False)
    handler.module_backends = {
        manifest.dht_prefix
        + ".0": SimpleNamespace(
            memory_cache=SimpleNamespace(bytes_left=4096), cache_bytes_per_token={"test": 16}, get_info=lambda: {}
        )
    }
    handler._allocate_cache = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("invalid probe allocated cache")
    )
    context = SimpleNamespace(remote_id=PEER)

    class Stub:
        _peer = PEER

        async def rpc_info(self, request):
            return await handler.rpc_info(request, context)

        async def rpc_forward(self, request):
            try:
                return await handler.rpc_forward(request, context)
            except Exception as exc:
                raise P2PHandlerError(str(exc)) from None

        async def rpc_inference(self, requests):
            async def responses():
                try:
                    async for response in handler.rpc_inference(requests, context):
                        yield response
                except Exception as exc:
                    raise P2PHandlerError(str(exc)) from None

            return responses()

    def health():
        return build_public_worker_health(
            manifest_digest=manifest.digest_id,
            start_block=0,
            end_block=1,
            admission_snapshot=state.snapshot(),
            ready=True,
            announcer_alive=True,
            handlers_alive=True,
            pools_alive=True,
        )

    async def exercise():
        if not real_transport:
            return await canary.Probe(
                Stub(), health, manifest, 0, policy, refill=0, step_timeout=handler.step_timeout
            ).run()
        server = client = None
        options = dict(
            host_maddrs=["/ip4/127.0.0.1/tcp/0"],
            auto_nat=False,
            conn_manager=False,
            nat_port_map=False,
            use_relay=False,
            tls=True,
            startup_timeout=10,
        )
        try:
            server = await canary.P2P.create(initial_peers=[], **options)
            handler.dht.peer_id = server.peer_id
            for method, request, stream in (
                ("rpc_info", canary.runtime_pb2.ExpertUID, False),
                ("rpc_forward", canary.runtime_pb2.ExpertRequest, False),
                ("rpc_inference", canary.runtime_pb2.ExpertRequest, True),
            ):
                await server.add_protobuf_handler(
                    TransformerConnectionHandler._get_handle_name(None, method),
                    getattr(handler, method),
                    request,
                    stream_input=stream,
                    stream_output=stream,
                )
            client = await canary.P2P.create(initial_peers=await server.get_visible_maddrs(), **options)
            stub = TransformerConnectionHandler.get_stub(client, server.peer_id)
            return await asyncio.wait_for(
                canary.Probe(stub, health, manifest, 0, policy, refill=0, step_timeout=handler.step_timeout).run(), 20
            )
        finally:
            for transport in (client, server):
                if transport is not None:
                    await asyncio.wait_for(transport.shutdown(), 5)
                    assert transport._child.returncode is not None

    if not enforce_peer_cap:
        with pytest.raises(canary.CanaryError, match="unexpected_rpc_rejection"):
            asyncio.run(exercise())
        assert state.snapshot()["active_sessions"] == 0
        return
    result = asyncio.run(exercise())
    assert len(result["checks"]) == 7
    assert result["rpc_calls"] == 20
    assert result["input_bytes"] < canary.MAX_INPUT_BYTES
    assert result["after"]["admission"]["active_sessions"] == 0
    assert result["after"]["admission"]["rejected_sessions"] == 1


def test_overload_does_not_count_as_malformed_rejection():
    class Stub:
        async def rpc_inference(self, requests):
            async def responses():
                raise P2PHandlerError(canary.PUBLIC_OVERLOAD_MESSAGE)
                yield

            return responses()

    probe = canary.Probe(Stub(), None, None, 0, None, refill=0, step_timeout=1)
    with pytest.raises(canary.CanaryError, match="unexpected_rpc_rejection"):
        asyncio.run(probe.reject(canary.runtime_pb2.ExpertRequest(), "metadata is invalid"))


@pytest.mark.parametrize(
    "elapsed,rejected,closure,error",
    [
        (1.0, 1, ConnectionResetError, None),
        (1.0, 1, StopAsyncIteration, None),
        (0.5, 1, ConnectionResetError, "idle_lease_released_before_timeout"),
        (0.5, 1, StopAsyncIteration, "idle_lease_released_before_timeout"),
        (1.0, 2, ConnectionResetError, "admission_counters_contaminated_or_missing"),
        (1.0, 1, "already_reset", "idle_stream_closed_before_producer_release"),
        (1.0, 1, RuntimeError, RuntimeError),
    ],
)
def test_idle_closure_requires_independent_timeout_and_counter_proof(elapsed, rejected, closure, error, monkeypatch):
    """A deterministic health clock isolates the proof required before closing the producer."""
    manifest = ModelManifest.load(MANIFEST)
    now = [0.0]
    released = []
    disconnected = asyncio.Event()

    class Stub:
        async def rpc_inference(self, requests):
            async def responses():
                if closure == "already_reset":
                    await disconnected.wait()
                    raise ConnectionResetError("reset before producer release")
                async for _ in requests:
                    raise AssertionError("idle producer sent a request")
                released.append(True)
                if closure is not StopAsyncIteration:
                    raise closure("synthetic transport closure")
                if False:
                    yield

            return responses()

    class Probe(canary.Probe):
        observations = 0

        async def info(self):
            return 256

        async def reject(self, request, expected, **kwargs):
            return

        async def wait_health(self, predicate, timeout=15):
            self.observations += 1
            active = self.observations in (2, 3)
            if self.observations >= 4:
                now[0] = elapsed
                disconnected.set()
                await asyncio.sleep(0)
            value = {
                "admission": {
                    "active_sessions": int(active),
                    "pending_pushes": 0,
                    "accepted_sessions": int(self.observations > 1),
                    "rejected_sessions": rejected if self.observations >= 4 else 0,
                }
            }
            assert predicate(value)
            return value

    monkeypatch.setattr(canary, "malformed_cases", lambda *args: [])
    probe = Probe(Stub(), None, manifest, 0, AdmissionPolicy(), refill=0, step_timeout=1, clock=lambda: now[0])
    if error is None:
        result = asyncio.run(probe.run())
        assert result["checks"][0]["transport_closure"] == (
            "end_of_stream" if closure is StopAsyncIteration else "connection_reset_after_idle_release"
        )
        assert released == [True]
    elif isinstance(error, str):
        with pytest.raises(canary.CanaryError, match=error):
            asyncio.run(probe.run())
        assert not released and not probe.checks
    else:
        with pytest.raises(error, match="synthetic transport closure"):
            asyncio.run(probe.run())
        assert not probe.checks


@pytest.mark.parametrize("expected", [canary.PUBLIC_OVERLOAD_MESSAGE, "metadata is invalid"])
def test_reset_during_admission_or_malformed_probe_is_not_accepted(expected):
    class Stub:
        async def rpc_inference(self, requests):
            async def responses():
                raise ConnectionResetError("unexpected reset")
                yield

            return responses()

    probe = canary.Probe(Stub(), None, None, 0, None, refill=0, step_timeout=1)
    with pytest.raises(ConnectionResetError, match="unexpected reset"):
        asyncio.run(probe.reject(canary.runtime_pb2.ExpertRequest(), expected))
