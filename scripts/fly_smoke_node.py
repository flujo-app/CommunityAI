"""Run one role of the private TinyLlama Fly Machines smoke swarm.

Roles are selected with ``FLY_SMOKE_ROLE``:

* ``bootstrap`` runs the private DHT bootstrap peer;
* ``worker`` serves the explicit range in ``FLY_SMOKE_BLOCKS``;
* ``client`` waits for complete coverage, generates tokens, checks exact parity,
  and emits a machine-readable ``FLY_SMOKE_RESULT=...`` line;
* ``multi-node-client`` validates two manifested routes through the official
  OpenAI client, node restart, LRU residency, and the edge benchmark; and
* ``local`` runs the one-process Linux CPU smoke test inside the image.

All networking stays on Fly's organization-private IPv6 network. The bootstrap
multiaddr passed to worker/client roles is supplied in ``FLY_SMOKE_INITIAL_PEER``.
"""

from __future__ import annotations

import json
import os
import resource
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import torch
from hivemind import DHT
from transformers import AutoModelForCausalLM, AutoTokenizer

import drift
from drift import AutoDistributedModelForCausalLM
from drift.data_structures import UID_DELIMITER
from drift.model_manifest import ManifestArtifactVerifier, ModelManifest, resolve_manifest_loading
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.dht import get_remote_module_infos

MODEL = "Maykeye/TinyLLama-v0"
NUM_BLOCKS = 8
PORT = 31337
DEFAULT_MANIFEST_PATH = "/workspace/model-manifest.json"
MULTI_MANIFEST_A_PATH = "/workspace/model-a.json"
MULTI_MANIFEST_B_PATH = "/workspace/model-b.json"


def log(message: str) -> None:
    print(f"[fly-smoke] {message}", flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for this role")
    return value


def private_ip() -> str:
    value = os.environ.get("FLY_PRIVATE_IP")
    if value:
        return value

    hostname = socket.gethostname()
    for candidate in (hostname, f"{hostname}.vm.internal"):
        try:
            addresses = socket.getaddrinfo(candidate, None, socket.AF_INET6)
        except socket.gaierror:
            continue
        if addresses:
            return addresses[0][4][0]
    raise RuntimeError("could not determine the Fly private IPv6 address")


def load_manifest() -> tuple[ModelManifest, str, str]:
    manifest_path = os.environ.get("FLY_SMOKE_MODEL_MANIFEST", DEFAULT_MANIFEST_PATH)
    manifest = ModelManifest.load(manifest_path)
    manifest.validate_runtime(drift.__version__)
    revision, dht_prefix = resolve_manifest_loading(
        manifest,
        model_name_or_path=MODEL,
        revision=None,
        dht_prefix=None,
    )
    return manifest, revision, dht_prefix


def run_bootstrap() -> None:
    ip = private_ip()
    log(f"role=bootstrap private_ip={ip}")
    identity_path = Path("/tmp/fly-smoke-bootstrap.id")
    args = [
        "drift",
        "dht",
        "--host_maddrs",
        f"/ip6/::/tcp/{PORT}",
        "--announce_maddrs",
        f"/ip6/{ip}/tcp/{PORT}",
        "--identity_path",
        str(identity_path),
        "--no_relay",
        "--refresh_period",
        "5",
    ]
    os.execvp(args[0], args)


def worker_args() -> list[str]:
    ip = private_ip()
    initial_peer = required_env("FLY_SMOKE_INITIAL_PEER")
    block_indices = required_env("FLY_SMOKE_BLOCKS")
    manifest_path = os.environ.get("FLY_SMOKE_MODEL_MANIFEST", DEFAULT_MANIFEST_PATH)
    return [
        "drift",
        "server",
        MODEL,
        "--model_manifest",
        manifest_path,
        "--identity_path",
        "/tmp/fly-smoke-worker.id",
        "--initial_peers",
        initial_peer,
        "--block_indices",
        block_indices,
        "--host_maddrs",
        f"/ip6/::/tcp/{PORT}",
        "--announce_maddrs",
        f"/ip6/{ip}/tcp/{PORT}",
        "--device",
        "cpu",
        "--torch_dtype",
        "float32",
        "--attn_implementation",
        "eager",
        "--quant_type",
        "none",
        "--attn_cache_tokens",
        os.environ.get("FLY_SMOKE_ATTN_CACHE_TOKENS", "1024"),
        "--max_batch_size",
        "64",
        "--max_chunk_size_bytes",
        str(16 * 1024 * 1024),
        "--throughput",
        os.environ.get("FLY_SMOKE_THROUGHPUT", "1.0"),
        "--update_period",
        "5",
        "--expiration",
        "30",
        "--request_timeout",
        "60",
        "--session_timeout",
        "60",
        "--step_timeout",
        "30",
        "--no_auto_relay",
    ]


def run_worker() -> None:
    manifest, revision, dht_prefix = load_manifest()
    ip = private_ip()
    initial_peer = required_env("FLY_SMOKE_INITIAL_PEER")
    block_indices = required_env("FLY_SMOKE_BLOCKS")
    log(
        f"role=worker blocks={block_indices} private_ip={ip} initial_peer={initial_peer} "
        f"revision={revision} manifest_digest={manifest.digest} dht_prefix={dht_prefix}"
    )
    args = worker_args()
    os.execvp(args[0], args)


def run_poisoned_worker() -> None:
    manifest, revision, dht_prefix = load_manifest()
    verifier = ManifestArtifactVerifier(manifest, MODEL, revision)
    snapshot_root = verifier.ensure_startup_metadata()
    config_artifact = manifest.artifacts_for_roles({"config"})[0]
    config_path = snapshot_root / config_artifact.path
    poisoned = bytearray(config_path.read_bytes())
    if not poisoned:
        raise RuntimeError("cannot poison an empty configuration artifact")
    poisoned[0] ^= 0x01
    config_path.write_bytes(poisoned)
    log(
        f"role=poisoned-worker corrupted={config_artifact.path} revision={revision} "
        f"manifest_digest={manifest.digest} dht_prefix={dht_prefix}"
    )
    args = worker_args()
    os.execvp(args[0], args)


def wait_for_coverage(
    dht: DHT,
    dht_prefix: str,
    manifest_digest: str,
    manifest_execution_profile: dict,
    timeout: float,
) -> list[int]:
    uids = [f"{dht_prefix}{UID_DELIMITER}{index}" for index in range(NUM_BLOCKS)]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        module_infos = get_remote_module_infos(
            dht,
            uids,
            manifest_digest=manifest_digest,
            manifest_execution_profile=manifest_execution_profile,
            latest=True,
        )
        replicas = [len(info.servers) for info in module_infos]
        log(f"coverage={sum(count > 0 for count in replicas)}/{NUM_BLOCKS} replicas={replicas}")
        if all(replicas):
            return replicas
        time.sleep(2)
    raise TimeoutError("the Fly swarm did not reach complete 0:8 block coverage")


def run_client() -> None:
    initial_peer = required_env("FLY_SMOKE_INITIAL_PEER")
    timeout = float(os.environ.get("FLY_SMOKE_TIMEOUT", "300"))
    max_new_tokens = int(os.environ.get("FLY_SMOKE_TOKENS", "8"))
    manifest, revision, dht_prefix = load_manifest()
    verifier = ManifestArtifactVerifier(manifest, MODEL, revision)
    config_source = verifier.ensure_startup_metadata(include_tokenizer=True)
    log(
        f"role=client initial_peer={initial_peer} timeout={timeout} revision={revision} "
        f"manifest_digest={manifest.digest} dht_prefix={dht_prefix}"
    )

    started = time.monotonic()
    dht = DHT(initial_peers=[initial_peer], client_mode=True, start=True)
    try:
        replicas = wait_for_coverage(dht, dht_prefix, manifest.digest, manifest.runtime.to_dict(), timeout)
    finally:
        dht.shutdown()
        dht.join()
    coverage_seconds = time.monotonic() - started

    config = AutoDistributedConfig.from_pretrained(
        config_source,
        local_files_only=True,
        dht_prefix=dht_prefix,
        initial_peers=[initial_peer],
        manifest_digest=manifest.digest,
        manifest_execution_profile=manifest.runtime.to_dict(),
        request_timeout=float(os.environ.get("FLY_SMOKE_REQUEST_TIMEOUT", "10")),
        max_retries=int(os.environ.get("FLY_SMOKE_MAX_RETRIES", "3")),
        min_backoff=0.1,
        max_backoff=1,
    )
    config._attn_implementation = "eager"
    tokenizer = AutoTokenizer.from_pretrained(config_source, local_files_only=True)
    model = AutoDistributedModelForCausalLM.from_pretrained(
        MODEL,
        config=config,
        revision=revision,
        artifact_verifier=verifier,
        torch_dtype=torch.float32,
    )
    inputs = tokenizer("Hello", return_tensors="pt")["input_ids"]

    log("first_token_start")
    first_started = time.monotonic()
    with torch.inference_mode():
        model.generate(inputs, max_new_tokens=1, do_sample=False)
    first_token_seconds = time.monotonic() - first_started

    log(f"generation_start tokens={max_new_tokens}")
    generation_started = time.monotonic()
    with torch.inference_mode():
        distributed = model.generate(
            inputs,
            max_new_tokens=max_new_tokens,
            min_new_tokens=max_new_tokens,
            do_sample=False,
        )
    generation_seconds = time.monotonic() - generation_started
    log("generation_complete")

    reference_started = time.monotonic()
    for artifact in manifest.artifacts_for_roles({"weight", "weight_index", "converted_weight", "quantized_weight"}):
        verifier.ensure_path(
            artifact.path,
            allowed_roles={"weight", "weight_index", "converted_weight", "quantized_weight"},
        )
    reference_model = AutoModelForCausalLM.from_pretrained(
        verifier.snapshot_root,
        local_files_only=True,
        dtype=torch.float32,
    )
    reference_model.eval()
    with torch.inference_mode():
        reference = reference_model.generate(
            inputs,
            max_new_tokens=max_new_tokens,
            min_new_tokens=max_new_tokens,
            do_sample=False,
        )
    reference_seconds = time.monotonic() - reference_started

    if not torch.equal(distributed.cpu(), reference.cpu()):
        raise AssertionError(
            f"distributed output differs from stock: distributed={distributed.tolist()} reference={reference.tolist()}"
        )

    result = {
        "model": MODEL,
        "revision": revision,
        "manifest_digest": manifest.digest,
        "dht_prefix": dht_prefix,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "coverage_seconds": round(coverage_seconds, 3),
        "replicas_per_block": replicas,
        "first_token_seconds": round(first_token_seconds, 3),
        "generation_seconds": round(generation_seconds, 3),
        "generated_tokens": max_new_tokens,
        "tokens_per_second": round(max_new_tokens / generation_seconds, 3),
        "reference_seconds": round(reference_seconds, 3),
        "output_ids": distributed.tolist(),
        "decoded": tokenizer.decode(distributed[0]),
        "exact_token_parity": True,
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    print(f"FLY_SMOKE_RESULT={json.dumps(result, sort_keys=True)}", flush=True)


def run_local() -> None:
    log("role=local: running the Linux CPU DHT/server/client parity smoke")
    subprocess.run(
        [
            sys.executable,
            "-u",
            "scripts/smoke_tinyllama_local_swarm.py",
            "--device",
            "cpu",
            "--torch-dtype",
            "float32",
            "--timeout",
            os.environ.get("FLY_SMOKE_TIMEOUT", "300"),
            "--block-indices",
            "0:8",
            "--model-manifest",
            os.environ.get("FLY_SMOKE_MODEL_MANIFEST", DEFAULT_MANIFEST_PATH),
        ],
        check=True,
    )


@contextmanager
def live_uvicorn(app):
    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("the multi-model node HTTP server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("the multi-model node HTTP server did not stop")


def wait_for_node_routes(client, model_ids: list[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/control/v1/status").json()
        routes = {item["id"]: item.get("route") for item in status["models"]}
        log(f"node_routes={json.dumps(routes, sort_keys=True)}")
        if all(routes.get(model_id, {}).get("status") == "complete" for model_id in model_ids):
            return status
        time.sleep(1)
    raise TimeoutError("the multi-model node did not discover complete routes")


def reference_completion(manifest: ModelManifest, prompt: str, max_new_tokens: int) -> str:
    verifier = ManifestArtifactVerifier(
        manifest,
        manifest.source.repository,
        manifest.source.revision,
        cache_dir="/tmp/multi-model-node-cache",
    )
    source = verifier.ensure_startup_metadata(include_tokenizer=True)
    tokenizer = AutoTokenizer.from_pretrained(source, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        manifest.source.repository,
        revision=manifest.source.revision,
        cache_dir="/tmp/multi-model-node-cache",
        dtype=getattr(torch, manifest.runtime.dtype),
        attn_implementation=manifest.runtime.attention_implementation,
    )
    model.eval()
    input_ids = tokenizer(prompt, return_tensors="pt")["input_ids"]
    with torch.inference_mode():
        output_ids = model.generate(input_ids, max_new_tokens=max_new_tokens, do_sample=False)
    return tokenizer.decode(output_ids[0, input_ids.shape[1] :], skip_special_tokens=True)


def run_multi_node_client() -> None:
    import httpx
    from openai import OpenAI

    from drift.cli.run_node import _build_model_manager
    from drift.node.config import NodeConfig, NodeModelConfig
    from drift.node.edge_benchmark import benchmark_client_runtime
    from drift.node.keys import ApiKeyStore
    from drift.node.loading import make_manifest_loader
    from drift.node.server import create_node_app

    initial_peer = required_env("FLY_SMOKE_INITIAL_PEER")
    timeout = float(os.environ.get("FLY_SMOKE_TIMEOUT", "300"))
    manifest_paths = [
        Path(os.environ.get("FLY_SMOKE_MANIFEST_A", MULTI_MANIFEST_A_PATH)),
        Path(os.environ.get("FLY_SMOKE_MANIFEST_B", MULTI_MANIFEST_B_PATH)),
    ]
    manifests = [ModelManifest.load(path) for path in manifest_paths]
    if manifests[0].digest == manifests[1].digest:
        raise ValueError("multi-model validation requires two distinct manifest digests")
    model_ids = [manifest.name for manifest in manifests]
    prompt = "Hello"
    max_new_tokens = 3
    api_key = f"drift_{secrets.token_urlsafe(32)}"
    control_key = f"drift_control_{secrets.token_urlsafe(32)}"

    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        discovery_update_period=1,
        discovery_startup_timeout=15,
        models=tuple(
            NodeModelConfig(
                manifest_path=path,
                initial_peers=(initial_peer,),
                cache_dir=Path("/tmp/multi-model-node-cache"),
                request_timeout=30,
                max_retries=3,
            )
            for path in manifest_paths
        ),
    )
    data_dir = Path(tempfile.mkdtemp(prefix="drift-fly-multi-node-"))
    key_path = data_dir / "api-keys.json"
    key_store = ApiKeyStore(key_path)
    key_store.ensure_key(api_key, label="fly-validation")
    edge_cache = Path("/tmp/edge-benchmark-cache")
    edge_measurement = benchmark_client_runtime(
        manifests[0],
        make_manifest_loader(
            manifests[0],
            initial_peers=(initial_peer,),
            cache_dir=str(edge_cache),
            request_timeout=30,
            max_retries=3,
        ),
        cache_dir=edge_cache,
        prompt=prompt,
        max_new_tokens=max_new_tokens,
    )
    expected = reference_completion(manifests[0], prompt, max_new_tokens)
    result = {
        "models": model_ids,
        "manifest_digests": [manifest.digest_id for manifest in manifests],
        "expected": expected,
        "edge_benchmark": edge_measurement,
    }

    def run_node_once(*, exercise_both: bool) -> dict:
        manager, _, discovery = _build_model_manager(config, token=None)
        app = create_node_app(
            manager,
            api_key_store=ApiKeyStore(key_path),
            control_keys=[control_key],
            host="127.0.0.1",
            port=0,
        )
        discovery.start()
        try:
            with live_uvicorn(app) as origin:
                control_headers = {"Authorization": f"Bearer {control_key}"}
                control = httpx.Client(base_url=origin, headers=control_headers, timeout=30)
                sdk = OpenAI(api_key=api_key, base_url=f"{origin}/v1", max_retries=0, timeout=60)
                if (
                    httpx.get(
                        f"{origin}/control/v1/status",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=30,
                    ).status_code
                    != 401
                ):
                    raise AssertionError("OpenAI client key unexpectedly authorized the control API")
                if control.get("/v1/models").status_code != 401:
                    raise AssertionError("control key unexpectedly authorized the OpenAI API")
                status_before = wait_for_node_routes(control, model_ids, timeout)
                listed = sorted(model.id for model in sdk.models.list().data)
                if listed != sorted(model_ids):
                    raise AssertionError(f"official SDK listed {listed}, expected {sorted(model_ids)}")

                first = sdk.completions.create(
                    model=model_ids[0], prompt=prompt, max_tokens=max_new_tokens, temperature=0
                )
                if first.choices[0].text != expected:
                    raise AssertionError(f"first model output {first.choices[0].text!r} != {expected!r}")
                first_status = control.get("/control/v1/status").json()
                states_after_first = {item["id"]: item["state"] for item in first_status["models"]}

                second_text = None
                states_after_second = None
                if exercise_both:
                    stream = sdk.completions.create(
                        model=model_ids[1],
                        prompt=prompt,
                        max_tokens=max_new_tokens,
                        temperature=0,
                        stream=True,
                    )
                    second_text = "".join(chunk.choices[0].text or "" for chunk in stream if chunk.choices)
                    if second_text != expected:
                        raise AssertionError(f"second model output {second_text!r} != {expected!r}")
                    second_status = control.get("/control/v1/status").json()
                    states_after_second = {item["id"]: item["state"] for item in second_status["models"]}
                    if states_after_second[model_ids[0]] != "known" or states_after_second[model_ids[1]] not in {
                        "ready",
                        "degraded",
                    }:
                        raise AssertionError(f"LRU residency did not move to the second model: {states_after_second}")
                sdk.close()
                control.close()
                return {
                    "routes_before_load": {item["id"]: item["route"] for item in status_before["models"]},
                    "listed": listed,
                    "first_completion": first.choices[0].text,
                    "states_after_first": states_after_first,
                    "second_stream": second_text,
                    "states_after_second": states_after_second,
                    "authorization_domains_separate": True,
                }
        finally:
            manager.shutdown()

    result["first_node"] = run_node_once(exercise_both=True)
    result["restarted_node"] = run_node_once(exercise_both=False)
    result["persistent_key_reused"] = ApiKeyStore(key_path).verify(api_key)
    result["exact_completion_parity"] = result["first_node"]["first_completion"] == expected
    result["restart_completion_parity"] = result["restarted_node"]["first_completion"] == expected
    result["client_max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"FLY_MULTI_NODE_RESULT={json.dumps(result, sort_keys=True)}", flush=True)


def main() -> None:
    role = os.environ.get("FLY_SMOKE_ROLE", "local")
    roles = {
        "bootstrap": run_bootstrap,
        "worker": run_worker,
        "poisoned-worker": run_poisoned_worker,
        "client": run_client,
        "multi-node-client": run_multi_node_client,
        "local": run_local,
    }
    try:
        run = roles[role]
    except KeyError as exc:
        raise ValueError(f"unknown FLY_SMOKE_ROLE={role!r}; expected one of {sorted(roles)}") from exc
    run()


if __name__ == "__main__":
    main()
