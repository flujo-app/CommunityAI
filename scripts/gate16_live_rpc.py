"""Bounded non-compute RPC canary against one explicitly selected, already-owned worker.

Run beside the worker's live public-health file. This script creates one temporary
local TLS p2pd client, never provisions a server, never loads weights, and sends no
valid inference tensor. It does not close Gate 16 or replace post-probe inference.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from hivemind.p2p import P2P, P2PHandlerError, PeerID
from hivemind.proto import runtime_pb2
from hivemind.utils.multiaddr import Multiaddr
from hivemind.utils.serializer import MSGPackSerializer

from drift.model_manifest import ModelManifest
from drift.protocol_identity import TRANSPORT_SECURITY
from drift.server.admission import PUBLIC_OVERLOAD_MESSAGE, AdmissionPolicy
from drift.server.handler import MAX_INFERENCE_METADATA_BYTES, TransformerConnectionHandler
from drift.server.health import MAX_HEALTH_STATE_BYTES, build_public_worker_health

MAX_RUN_SECONDS = 600
MAX_RPC_CALLS = 20
MAX_INPUT_BYTES = 128 * 1024


class CanaryError(RuntimeError):
    """A sanitized, fixed-code canary failure."""


def require(condition, code):
    if not condition:
        raise CanaryError(code)


def read_json(path, maximum):
    require(path.is_file() and not path.is_symlink(), "unsafe_input_file")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum, "input_size_limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=unique)


def load_policy(path):
    source = read_json(path, 4096)
    require(set(source) == {"schema_version", "admission", "step_timeout", "session_timeout"}, "policy_schema")
    require(type(source["schema_version"]) is int and source["schema_version"] == 1, "policy_version")
    policy = AdmissionPolicy(**source["admission"])
    require(policy.max_active_sessions_per_peer == 1, "probe_requires_one_session_per_peer")
    require(policy.max_active_sessions > 1, "probe_requires_spare_global_session")
    require(policy.allow_training_rpcs is False, "training_must_be_disabled")
    for field in ("step_timeout", "session_timeout"):
        value = source[field]
        require(type(value) in (float, int) and math.isfinite(value) and 0 < value <= 60, "timeout_bounds")
    require(source["step_timeout"] < source["session_timeout"], "step_must_precede_session_timeout")
    refill = max(policy.peer_session_burst / policy.peer_session_rate, 1 / policy.global_session_rate)
    require(refill <= 30, "refill_exceeds_probe_budget")
    require(refill + 2 < source["step_timeout"], "step_timeout_must_allow_refilled_admission_probe")
    return source, policy, refill


def exact_peer(value):
    require(isinstance(value, str) and len(value) <= 2048, "peer_address_bounds")
    address = Multiaddr(value)
    protocols = tuple(protocol.name for protocol in address.protocols())
    require(protocols in (("ip4", "tcp", "p2p"), ("ip6", "tcp", "p2p"), ("dns4", "tcp", "p2p")), "peer_protocols")
    require(str(address) == value, "peer_address_not_canonical")
    require(1 <= int(address.value_for_protocol("tcp")) <= 65535, "peer_port")
    return PeerID.from_base58(address.value_for_protocol("p2p"))


def read_health(path, manifest_digest, block, *, maximum_age=15, now=None):
    source = read_json(path, MAX_HEALTH_STATE_BYTES)
    require(
        set(source)
        == {
            "schema_version",
            "scope",
            "observed_at",
            "worker_healthy",
            "route",
            "admission_available",
            "admission",
            "components",
        },
        "health_schema",
    )
    require(type(source["schema_version"]) is int and source["schema_version"] == 1, "health_version")
    canonical = build_public_worker_health(
        **source["route"],
        admission_snapshot=source["admission"],
        observed_at=source["observed_at"],
        **source["components"],
    )
    require(source == canonical and canonical["worker_healthy"], "health_unavailable")
    require(source["route"]["manifest_digest"] == manifest_digest, "health_manifest_mismatch")
    require(source["route"]["start_block"] <= block < source["route"]["end_block"], "health_block_mismatch")
    stamp = datetime.fromisoformat(source["observed_at"].replace("Z", "+00:00")).timestamp()
    age = (time.time() if now is None else now) - stamp
    require(0 <= age <= maximum_age, "health_stale")
    return source


def malformed_cases(uid, wire_digest):
    common = {"manifest_digest": wire_digest, "max_length": 1}
    wrong_digest = "0" * 64 if wire_digest != "0" * 64 else "1" * 64
    values = (
        ("oversized_metadata", b"x" * (MAX_INFERENCE_METADATA_BYTES + 1), "too large"),
        (
            "manifest_mismatch",
            MSGPackSerializer.dumps(dict(common, manifest_digest=wrong_digest)),
            "Manifest digest mismatch",
        ),
        ("non_dictionary_metadata", MSGPackSerializer.dumps([]), "metadata is invalid"),
        (
            "non_finite_allocation_timeout",
            MSGPackSerializer.dumps(dict(common, alloc_timeout=float("nan"))),
            "metadata is invalid",
        ),
        ("negative_maximum_length", MSGPackSerializer.dumps(dict(common, max_length=-1)), "Cannot allocate KV cache"),
    )
    return [
        (name, runtime_pb2.ExpertRequest(uid=uid, metadata=metadata), expected) for name, metadata, expected in values
    ]


class Probe:
    def __init__(self, stub, health, manifest, block, policy, *, refill, step_timeout, clock=time.monotonic):
        self.stub, self.health, self.manifest, self.block = stub, health, manifest, block
        self.policy, self.refill, self.step_timeout = policy, refill, step_timeout
        self.clock = clock
        self.calls, self.input_bytes = 0, 0
        self.checks = []
        self.baseline = None
        self.current_case = "baseline"

    def count(self, message=None):
        self.calls += 1
        if message is not None:
            self.input_bytes += message.ByteSize()
        require(self.calls <= MAX_RPC_CALLS and self.input_bytes <= MAX_INPUT_BYTES, "rpc_budget_exceeded")

    async def wait_health(self, predicate, timeout=15):
        deadline = self.clock() + timeout
        while True:
            value = self.health()
            admission = value["admission"]
            require(admission["active_sessions"] <= self.policy.max_active_sessions, "active_session_bound")
            require(admission["tracked_peers"] <= self.policy.max_tracked_peers, "tracked_peer_bound")
            require(admission["pending_pushes"] <= self.policy.max_pending_pushes, "pending_push_bound")
            if self.baseline is not None:
                require(
                    admission["accepted_sessions"] >= self.baseline["admission"]["accepted_sessions"],
                    "worker_counter_reset",
                )
                require(
                    admission["rejected_sessions"] >= self.baseline["admission"]["rejected_sessions"],
                    "worker_counter_reset",
                )
            if predicate(value):
                return value
            require(self.clock() < deadline, "health_observation_deadline")
            await asyncio.sleep(0.2)

    async def info(self):
        message = runtime_pb2.ExpertUID(uid=f"{self.manifest.dht_prefix}.{self.block}")
        self.count(message)
        reply = await asyncio.wait_for(self.stub.rpc_info(message), 10)
        require(len(reply.serialized_info) <= 128 * 1024, "rpc_info_size")
        value = MSGPackSerializer.loads(reply.serialized_info)
        require(value["manifest_digest"] == self.manifest.digest, "rpc_manifest_mismatch")
        require(value["transport_security"] == TRANSPORT_SECURITY, "transport_security_mismatch")
        require(value["server_peer_id"] == self.stub._peer.to_base58(), "rpc_peer_mismatch")
        count = value["cache_tokens_available"]
        require(type(count) is int and count >= 0, "invalid_cache_counter")
        return count

    async def reject(self, message, expected, *, training=False):
        self.count(message)
        stream = None

        async def requests():
            yield message

        try:
            if training:
                await asyncio.wait_for(self.stub.rpc_forward(message), 10)
            else:
                stream = await asyncio.wait_for(self.stub.rpc_inference(requests()), 10)
                await asyncio.wait_for(anext(stream), 10)
        except P2PHandlerError as exc:
            # Keep transport errors private; only the fixed expected category is
            # exported. Overload must not masquerade as malformed-input rejection.
            require(expected in str(exc), "unexpected_rpc_rejection")
            return
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stream.aclose(), 2)
        raise CanaryError("invalid_request_accepted")

    async def run(self):
        self.baseline = await self.wait_health(
            lambda v: v["admission"]["active_sessions"] == 0 and v["admission"]["pending_pushes"] == 0
        )
        available = await self.info()
        self.current_case = "idle_stream_and_per_peer_admission"
        gate = asyncio.Event()

        async def idle():
            await gate.wait()
            if False:
                yield runtime_pb2.ExpertRequest()

        self.count()
        stream = await asyncio.wait_for(self.stub.rpc_inference(idle()), 10)
        opened = self.clock()
        next_reply = asyncio.create_task(anext(stream))
        try:
            await self.wait_health(lambda v: v["admission"]["active_sessions"] == 1, timeout=min(15, self.step_timeout))
            # Refill both buckets before the second stream so rate limiting cannot
            # masquerade as enforcement of the per-peer active-session ceiling.
            await asyncio.sleep(self.refill + 0.1)
            require(not next_reply.done(), "idle_lease_expired_before_admission_probe")
            await self.wait_health(lambda v: v["admission"]["active_sessions"] == 1, timeout=1)
            await self.reject(runtime_pb2.ExpertRequest(), PUBLIC_OVERLOAD_MESSAGE)
            after = await self.wait_health(
                lambda v: v["admission"]["active_sessions"] == 0,
                timeout=max(0, self.step_timeout + 5 - (self.clock() - opened)),
            )
            elapsed = self.clock() - opened
            require(elapsed >= self.step_timeout, "idle_lease_released_before_timeout")
            require(elapsed <= self.step_timeout + 5, "idle_timeout_exceeded")
            require(
                after["admission"]["rejected_sessions"] == self.baseline["admission"]["rejected_sessions"] + 1
                and after["admission"]["accepted_sessions"] == self.baseline["admission"]["accepted_sessions"] + 1,
                "admission_counters_contaminated_or_missing",
            )
            if next_reply.done():
                require(
                    not next_reply.cancelled() and isinstance(next_reply.exception(), StopAsyncIteration),
                    "idle_stream_closed_before_producer_release",
                )
            # Hivemind waits for the request producer after the server closes.
            # Finish it only after health proves the server released its lease,
            # so a client end-of-stream cannot falsely establish idle expiry.
            gate.set()
            try:
                await asyncio.wait_for(next_reply, 5)
            except StopAsyncIteration:
                closure = "end_of_stream"
            except ConnectionResetError:
                # Upstream Linux Hivemind can fail writing END_OF_STREAM to the
                # expired server stream. Only this post-proof producer release
                # may accept a reset; admission/malformed RPCs still fail on it.
                closure = "connection_reset_after_idle_release"
            else:
                raise CanaryError("idle_stream_produced_output")
            self.checks.append(
                {
                    "case": "idle_stream_and_per_peer_admission",
                    "result": "passed",
                    "duration_seconds": round(elapsed, 3),
                    "transport_closure": closure,
                }
            )
        finally:
            next_reply.cancel()
            with contextlib.suppress(BaseException):
                await next_reply
            gate.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stream.aclose(), 2)

        uid = f"{self.manifest.dht_prefix}.{self.block}"
        for name, message, expected in malformed_cases(uid, self.manifest.digest):
            self.current_case = name
            await asyncio.sleep(self.refill + 0.1)
            before = await self.info()
            began = self.clock()
            await self.reject(message, expected)
            await self.wait_health(lambda v: v["admission"]["active_sessions"] == 0)
            require(await self.info() == before == available, "cache_capacity_changed")
            self.checks.append({"case": name, "result": "passed", "duration_seconds": round(self.clock() - began, 3)})
        self.current_case = "training_forward_disabled"
        await self.reject(runtime_pb2.ExpertRequest(), "training RPCs are disabled", training=True)
        self.checks.append({"case": "training_forward_disabled", "result": "passed"})
        final = await self.wait_health(
            lambda v: v["admission"]["active_sessions"] == 0 and v["admission"]["pending_pushes"] == 0
        )
        require(await self.info() == available, "final_cache_capacity_changed")
        return {
            "checks": self.checks,
            "rpc_calls": self.calls,
            "input_bytes": self.input_bytes,
            "before": self.baseline,
            "after": final,
        }


async def run(args):
    manifest = ModelManifest.load(args.manifest)
    require(manifest.digest_id == args.expected_manifest_digest, "manifest_identity_mismatch")
    require(0 <= args.block < manifest.model.num_blocks, "block_bounds")
    peer = exact_peer(args.worker_multiaddr)
    source, policy, refill = load_policy(args.policy)
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", args.worker_label) is not None, "worker_label")
    health = lambda: read_health(args.health, manifest.digest_id, args.block)
    health()  # Refuse missing/stale health before any network connection.
    args.output.mkdir(parents=True, exist_ok=False)
    result = {
        "schema_version": 1,
        "result": "failed",
        "scope": "exact-worker-non-compute-rpc-canary",
        "executed": args.execute,
        "complete_gate16": False,
        "worker_label": args.worker_label,
        "target_binding_sha256": hashlib.sha256(
            json.dumps(
                {
                    "peer": args.worker_multiaddr,
                    "health": str(args.health.resolve()),
                    "block": args.block,
                    "manifest": manifest.digest_id,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "manifest_digest": manifest.digest_id,
        "policy": source,
        "policy_source": "operator-supplied effective launch settings; per-peer/idle behavior exercised below",
        "limits": {
            "operation_seconds": MAX_RUN_SECONDS,
            "cleanup_seconds": 80,
            "rpc_calls": MAX_RPC_CALLS,
            "input_bytes": MAX_INPUT_BYTES,
            "model_executions": 0,
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": [
            "No weights, valid inference tensors, inference recovery, UI disclosure, or catalog action in this RPC probe.",
            "Runs beside a live worker health file; no SSH, provisioning, worker stop or public catalog mutation.",
            "A client timeout is a failure; final worker health and owned-client cleanup are recorded separately.",
            "The probe exercises the per-peer active limit and idle expiry, not global saturation or identity churn.",
            "Health-to-peer pairing is operator supplied; public health does not include an authenticated peer identity.",
        ],
    }
    if not args.execute:
        result.update(result="preflight-passed", network_connections=0, executed=False)
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
    # The standalone driver owns every newly spawned child. Refuse pre-existing
    # children so final cleanup cannot accidentally include another workload.
    require(not psutil.Process().children(recursive=True), "driver_has_existing_children")
    p2p = None
    probe = None
    began = time.monotonic()
    try:
        async with asyncio.timeout(MAX_RUN_SECONDS):
            p2p = await P2P.create(
                initial_peers=[args.worker_multiaddr],
                host_maddrs=["/ip4/127.0.0.1/tcp/0"],
                dht_mode="client",
                auto_nat=False,
                conn_manager=False,
                nat_port_map=False,
                use_relay=False,
                use_ipfs=False,
                tls=True,
                startup_timeout=15,
                persistent_conn_max_msg_size=128 * 1024,
            )
            stub = TransformerConnectionHandler.get_stub(p2p, peer)
            probe = Probe(
                stub, health, manifest, args.block, policy, refill=refill, step_timeout=source["step_timeout"]
            )
            result.update(await probe.run())
            result["result"] = "passed"
    except BaseException as exc:
        result["error_type"] = type(exc).__name__
        if isinstance(exc, CanaryError):
            result["error_code"] = str(exc)
        if probe is not None:
            result.update(checks=probe.checks, rpc_calls=probe.calls, input_bytes=probe.input_bytes)
            result["failed_case"] = probe.current_case
    finally:
        cleanup = {"owned_client_stopped": False, "worker_sessions_released": False}
        try:
            owned_children = psutil.Process().children(recursive=True)
            if p2p is not None:
                try:
                    await asyncio.wait_for(p2p.shutdown(), 10)
                except BaseException:
                    result["result"] = "failed"
            # These handles refer only to this standalone driver's descendants;
            # psutil's signal methods reject PID reuse. Never select by image name.
            for child in owned_children:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.terminate()
            _, alive = psutil.wait_procs(owned_children, timeout=2)
            for child in alive:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            _, alive = psutil.wait_procs(alive, timeout=3)
            cleanup["owned_client_stopped"] = not alive and not psutil.Process().children(recursive=True)
        except BaseException as exc:
            result["result"] = "failed"
            result["client_cleanup_error_type"] = type(exc).__name__
        try:
            until = time.monotonic() + source["session_timeout"] + 5
            while True:
                last = health()
                if last["admission"]["active_sessions"] == 0 and last["admission"]["pending_pushes"] == 0:
                    cleanup["worker_sessions_released"] = True
                    break
                require(time.monotonic() < until, "cleanup_session_deadline")
                await asyncio.sleep(0.2)
        except BaseException:
            result["result"] = "failed"
        if not all(cleanup.values()):
            result["result"] = "failed"
        result["cleanup"] = cleanup
        result["duration_seconds"] = round(time.monotonic() - began, 3)
        result["privacy"] = {
            "raw_rpc_errors_exported": False,
            "peer_identities_exported": False,
            "credentials_created": False,
            "prompts_or_outputs_exported": False,
        }
        (args.output / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-manifest-digest", required=True)
    parser.add_argument("--worker-multiaddr", required=True)
    parser.add_argument("--worker-label", required=True)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--health", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Send the bounded probe to the explicitly selected owned worker; default only validates local preflight",
    )
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({"result": result["result"], "complete_gate16": False}))
    if result["result"] not in ("passed", "preflight-passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
