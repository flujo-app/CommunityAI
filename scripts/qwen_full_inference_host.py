"""Linux host jobs for the isolated, manifested Qwen full-inference swarm."""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
from pathlib import Path

ROOT = Path("/srv/q38")
SOURCE = Path("/opt/q38/source")
MANIFEST = SOURCE / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json"


def write(name, value):
    value = dict(value, observed_at_unix=time.time())
    path = ROOT / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)
    print(json.dumps(value), flush=True)


def bootstrap(config):
    from hivemind import DHT

    dht = DHT(
        initial_peers=[],
        host_maddrs=["/ip4/0.0.0.0/tcp/31330"],
        announce_maddrs=[f"/ip4/{config['ip']}/tcp/{config.get('public_port', 31330)}"],
        client_mode=False,
        start=True,
        tls=True,
    )
    try:
        write("bootstrap.json", {"peers": [str(a) for a in dht.get_visible_maddrs()]})
        while dht.is_alive():
            time.sleep(5)
        raise RuntimeError("bootstrap DHT exited")
    finally:
        dht.shutdown()


def worker(config):
    import torch

    from drift.cli.run_server import build_parser, serve, server_from_args

    torch.set_num_threads(4)
    argv = [
        "Qwen/Qwen3.8-27B-FP8",
        "--model_manifest",
        str(MANIFEST),
        "--block_indices",
        config["span"],
        "--device",
        config.get("device", "cpu"),
        "--cache_dir",
        str(ROOT / "cache"),
        "--max_disk_space",
        "24GiB",
        "--torch_dtype",
        "bfloat16",
        "--quant_type",
        "fp8_dequant",
        "--attn_implementation",
        "eager",
        "--throughput",
        "0.01",
        "--num_handlers",
        "1",
        "--inference_max_length",
        "64",
        "--attn_cache_tokens",
        "128",
        "--max_batch_size",
        "64",
        "--update_period",
        "10",
        "--expiration",
        "40",
        "--request_timeout",
        "1800",
        "--session_timeout",
        "14400",
        "--step_timeout",
        "3600",
        "--ready_timeout",
        "300",
        "--balance_quality",
        "0",
        "--no_auto_relay",
        "--host_maddrs",
        "/ip4/0.0.0.0/tcp/31330",
        "--announce_maddrs",
        f"/ip4/{config['ip']}/tcp/31330",
        "--health_state_path",
        str(ROOT / "health.json"),
        "--identity_path",
        str(ROOT / "identity.key"),
        "--initial_peers",
        *config["peers"],
    ]
    parsed = vars(build_parser().parse_args(argv))
    parsed.pop("config", None)
    server = server_from_args(parsed)
    hardware = {"device": str(server.device), "dtype": str(server.torch_dtype)}
    if server.device.type == "cuda":
        hardware.update(
            gpu_name=torch.cuda.get_device_name(server.device),
            gpu_memory_bytes=torch.cuda.get_device_properties(server.device).total_memory,
            compute_capability=list(torch.cuda.get_device_capability(server.device)),
        )
    write(
        "worker.json",
        {
            "span": config["span"],
            "peer_id": str(server.dht.peer_id),
            "hardware": hardware,
            "peers": [str(a) for a in server.dht.get_visible_maddrs()],
        },
    )
    serve(server, model="Qwen/Qwen3.8-27B-FP8")


def route(session):
    return [
        {"start": s.span.start, "end": s.span.end, "peer_id": str(s.span.peer_id), "session_id": s.session_id}
        for s in session._server_sessions
    ]


def gpu_probe(config):
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("the requested GPU is unavailable")
    matrix = torch.ones(256, 256, device="cuda", dtype=torch.bfloat16)
    product = matrix @ matrix
    convolution = torch.nn.functional.conv1d(
        torch.ones(1, 16, 8, device="cuda", dtype=torch.bfloat16),
        torch.ones(16, 16, 4, device="cuda", dtype=torch.bfloat16),
    )
    torch.cuda.synchronize()
    if not torch.isfinite(product).all() or not torch.isfinite(convolution).all():
        raise RuntimeError("GPU BF16 kernels produced non-finite values")
    write(
        "gpu-probe.json",
        {
            "result": "passed",
            "gpu_name": torch.cuda.get_device_name(),
            "memory_bytes": torch.cuda.get_device_properties(0).total_memory,
            "compute_capability": list(torch.cuda.get_device_capability()),
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "bf16_matmul": True,
            "bf16_convolution": True,
        },
    )


def client(config):
    import hivemind
    import torch
    import transformers
    from transformers import AutoTokenizer

    from drift import AutoDistributedModelForCausalLM
    from drift.model_manifest import ManifestArtifactVerifier, ModelManifest
    from drift.node.loading import _runtime_closer

    torch.set_num_threads(4)
    torch.manual_seed(0)
    manifest = ModelManifest.load(MANIFEST)
    verifier = ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        token=False,
        cache_dir=str(ROOT / "cache"),
    )
    write("client-status.json", {"phase": "loading-client"})
    verifier.ensure_startup_metadata(include_tokenizer=True)
    tokenizer = AutoTokenizer.from_pretrained(verifier.snapshot_root, local_files_only=True)
    model = AutoDistributedModelForCausalLM.from_pretrained(
        manifest.source.repository,
        revision=manifest.source.revision,
        token=False,
        initial_peers=config["peers"],
        dht_prefix=manifest.dht_prefix,
        manifest_digest=manifest.digest,
        manifest_execution_profile=manifest.runtime.to_dict(),
        torch_dtype=torch.bfloat16,
        artifact_verifier=verifier,
        request_timeout=config.get("request_timeout", 180),
        connect_timeout=30,
        max_retries=240,
        min_backoff=2,
        max_backoff=10,
        update_period=5,
        use_server_to_server=True,
    ).eval()
    try:
        prompt = "The capital of France is"
        inputs = tokenizer(prompt, return_tensors="pt")["input_ids"]
        generation = dict(do_sample=False, min_new_tokens=3, max_new_tokens=3, pad_token_id=tokenizer.eos_token_id)
        write("client-status.json", {"phase": "baseline", "prompt_tokens": inputs.shape[1]})
        started = time.monotonic()
        with torch.inference_mode(), model.inference_session(max_length=64) as session:
            output = model.generate(inputs, **generation)
            baseline_route = route(session)
        baseline = {
            "token_ids": output[0, inputs.shape[1] :].tolist(),
            "text": tokenizer.decode(output[0, inputs.shape[1] :]),
            "route": baseline_route,
            "seconds": time.monotonic() - started,
        }
        write("baseline.json", baseline)
        if config.get("run_recovery", True) is False:
            write(
                "client-result.json",
                {
                    "result": "passed",
                    "manifest_digest": manifest.digest_id,
                    "model_revision": manifest.source.revision,
                    "baseline": baseline,
                    "recovery": None,
                    "versions": {
                        "python": platform.python_version(),
                        "torch": torch.__version__,
                        "transformers": transformers.__version__,
                        "hivemind": hivemind.__version__,
                    },
                },
            )
            return
        with torch.inference_mode(), model.inference_session(max_length=64) as session:
            first = model.generate(
                inputs, do_sample=False, min_new_tokens=1, max_new_tokens=1, pad_token_id=tokenizer.eos_token_id
            )
            before = route(session)
            position = session.position
            write("recovery-ready.json", {"route": before, "position": position, "first_token_id": first[0, -1].item()})
            deadline = time.monotonic() + 5400
            while not (ROOT / "continue-recovery").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("replacement did not become ready")
                time.sleep(2)
            started = time.monotonic()
            # The exact same InferenceSession and history survive the worker loss.
            output = model.generate(
                do_sample=False, min_new_tokens=2, max_new_tokens=2, pad_token_id=tokenizer.eos_token_id
            )
            output = session.output_ids
            after = route(session)
            recovery = {
                "token_ids": output[0, inputs.shape[1] :].tolist(),
                "text": tokenizer.decode(output[0, inputs.shape[1] :]),
                "before_route": before,
                "after_route": after,
                "position_before": position,
                "position_after": session.position,
                "same_session": True,
                "seconds": time.monotonic() - started,
            }
        recovery["matches_baseline"] = recovery["token_ids"] == baseline["token_ids"]
        write("recovery.json", recovery)
        if not recovery["matches_baseline"]:
            raise RuntimeError("recovery tokens differ from uninterrupted generation")
        write(
            "client-result.json",
            {
                "result": "passed",
                "manifest_digest": manifest.digest_id,
                "model_revision": manifest.source.revision,
                "baseline": baseline,
                "recovery": recovery,
                "versions": {
                    "python": platform.python_version(),
                    "torch": torch.__version__,
                    "transformers": transformers.__version__,
                    "hivemind": hivemind.__version__,
                },
            },
        )
    finally:
        _runtime_closer(model)()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["bootstrap", "worker", "client", "gpu_probe"])
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "config.json").read_text())
    try:
        globals()[args.role](config)
    except BaseException as exc:
        write(f"{args.role}-error.json", {"error": type(exc).__name__, "message": str(exc)})
        raise


if __name__ == "__main__":
    main()
