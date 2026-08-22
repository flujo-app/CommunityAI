"""Run one role of the private TinyLlama Fly Machines smoke swarm.

Roles are selected with ``FLY_SMOKE_ROLE``:

* ``bootstrap`` runs the private DHT bootstrap peer;
* ``worker`` serves the explicit range in ``FLY_SMOKE_BLOCKS``;
* ``client`` waits for complete coverage, generates tokens, checks exact parity,
  and emits a machine-readable ``FLY_SMOKE_RESULT=...`` line;
* ``local`` runs the one-process Linux CPU smoke test inside the image.

All networking stays on Fly's organization-private IPv6 network. The bootstrap
multiaddr passed to worker/client roles is supplied in ``FLY_SMOKE_INITIAL_PEER``.
"""

from __future__ import annotations

import json
import os
import resource
import socket
import subprocess
import sys
import time
from pathlib import Path

import torch
from hivemind import DHT
from transformers import AutoModelForCausalLM, AutoTokenizer

from drift import AutoDistributedModelForCausalLM
from drift.data_structures import UID_DELIMITER
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.dht import get_remote_module_infos

MODEL = "Maykeye/TinyLLama-v0"
DHT_PREFIX = "_fly_tinyllama_v0_smoke"
NUM_BLOCKS = 8
PORT = 31337


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


def run_worker() -> None:
    ip = private_ip()
    initial_peer = required_env("FLY_SMOKE_INITIAL_PEER")
    block_indices = required_env("FLY_SMOKE_BLOCKS")
    log(f"role=worker blocks={block_indices} private_ip={ip} initial_peer={initial_peer}")
    args = [
        "drift",
        "server",
        MODEL,
        "--initial_peers",
        initial_peer,
        "--block_indices",
        block_indices,
        "--dht_prefix",
        DHT_PREFIX,
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
    os.execvp(args[0], args)


def wait_for_coverage(dht: DHT, timeout: float) -> list[int]:
    uids = [f"{DHT_PREFIX}{UID_DELIMITER}{index}" for index in range(NUM_BLOCKS)]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        module_infos = get_remote_module_infos(dht, uids, latest=True)
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
    log(f"role=client initial_peer={initial_peer} timeout={timeout}")

    started = time.monotonic()
    dht = DHT(initial_peers=[initial_peer], client_mode=True, start=True)
    try:
        replicas = wait_for_coverage(dht, timeout)
    finally:
        dht.shutdown()
        dht.join()
    coverage_seconds = time.monotonic() - started

    config = AutoDistributedConfig.from_pretrained(
        MODEL,
        dht_prefix=DHT_PREFIX,
        initial_peers=[initial_peer],
        request_timeout=float(os.environ.get("FLY_SMOKE_REQUEST_TIMEOUT", "10")),
        max_retries=int(os.environ.get("FLY_SMOKE_MAX_RETRIES", "3")),
        min_backoff=0.1,
        max_backoff=1,
    )
    config._attn_implementation = "eager"
    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    model = AutoDistributedModelForCausalLM.from_pretrained(
        MODEL,
        config=config,
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
    reference_model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=torch.float32)
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
        ],
        check=True,
    )


def main() -> None:
    role = os.environ.get("FLY_SMOKE_ROLE", "local")
    roles = {
        "bootstrap": run_bootstrap,
        "worker": run_worker,
        "client": run_client,
        "local": run_local,
    }
    try:
        run = roles[role]
    except KeyError as exc:
        raise ValueError(f"unknown FLY_SMOKE_ROLE={role!r}; expected one of {sorted(roles)}") from exc
    run()


if __name__ == "__main__":
    main()
