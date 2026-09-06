"""Compare the full manifested model with stock Transformers, including cached decoding."""

import concurrent.futures
import gc
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/srv/q38")
SOURCE = Path("/opt/q38/source")
MANIFEST = SOURCE / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json"
PROMPTS = ("The capital of France is", "Two plus three equals", "In one sentence, explain why the sky looks blue.")
# Declared before running either implementation. Compare every vocabulary logit
# and require the same greedy token at all nine prefill/cached-decode positions.
ATOL, RTOL, STEPS = 0.5, 0.01, 3


def write(name, value):
    value = dict(value, time=time.time())
    path = ROOT / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)
    print(json.dumps({"phase": name, "detail": value.get("phase", value.get("result"))}), flush=True)


def main():
    import torch
    import transformers
    from hivemind import DHT
    from transformers import AutoModelForImageTextToText, AutoTokenizer, FineGrainedFP8Config

    from drift import AutoDistributedModelForCausalLM
    from drift.model_manifest import ManifestArtifactVerifier, ModelManifest
    from drift.node.loading import _runtime_closer

    torch.set_num_threads(4)
    manifest = ModelManifest.load(MANIFEST)
    verifier = ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        token=False,
        cache_dir=str(ROOT / "cache"),
    )
    result = {
        "result": "failed",
        "scope": "stock-versus-four-RPC-worker numerical reference on one CPU host",
        "manifest_digest": manifest.digest_id,
        "revision": manifest.source.revision,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "atol": ATOL,
        "rtol": RTOL,
        "cached_decode_steps_per_prompt": STEPS - 1,
        "prompts": list(PROMPTS),
        "cross_host_qualification": False,
        "checks": [],
    }
    workers, logs, dht, remote, stock = [], [], None, None, None
    try:
        write("reference-status.json", {"phase": "verify-all-artifacts"})
        # Bind the verified snapshot before parallel downloads. This verifier's
        # first materialization establishes mutable root state and is serial.
        verifier.ensure_startup_metadata(include_tokenizer=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda a: verifier.ensure_path(a.path), manifest.artifacts))
        tokenizer = AutoTokenizer.from_pretrained(verifier.snapshot_root, local_files_only=True)
        write("reference-status.json", {"phase": "load-stock-transformers"})
        stock = AutoModelForImageTextToText.from_pretrained(
            verifier.snapshot_root,
            local_files_only=True,
            trust_remote_code=False,
            dtype=torch.bfloat16,
            device_map="cpu",
            attn_implementation="eager",
            quantization_config=FineGrainedFP8Config(dequantize=True),
        ).eval()
        result["stock_class"] = stock.__class__.__name__
        result["stock_dequantizer"] = "Transformers FineGrainedFP8Config(dequantize=True)"
        references = []
        with torch.inference_mode():
            for index, prompt in enumerate(PROMPTS):
                inputs = tokenizer(prompt, return_tensors="pt").input_ids
                cache, logits, tokens = None, [], []
                current = inputs
                for step in range(STEPS):
                    started = time.monotonic()
                    output = stock(input_ids=current, past_key_values=cache, use_cache=True, logits_to_keep=1)
                    cache = output.past_key_values
                    values = output.logits[0, -1].float().cpu().clone()
                    assert torch.isfinite(values).all()
                    token = int(values.argmax())
                    logits.append(values)
                    tokens.append(token)
                    current = torch.tensor([[token]])
                    write(
                        "reference-status.json",
                        {
                            "phase": "stock-forward",
                            "prompt_index": index,
                            "step": step,
                            "seconds": time.monotonic() - started,
                        },
                    )
                references.append((inputs, logits, tokens))
                del cache, output
        stock = None
        gc.collect()
        # Stock weights are released before allocating worker weights, so the
        # numerical comparison fits inside this explicitly bounded 96 GiB host.
        dht = DHT(initial_peers=[], host_maddrs=["/ip4/127.0.0.1/tcp/31330"], start=True, tls=True)
        peers = [str(a) for a in dht.get_visible_maddrs()]
        for index in range(4):
            directory = ROOT / f"reference-worker-{index}"
            directory.mkdir(exist_ok=False)
            log = (directory / "worker.log").open("wb")
            logs.append(log)
            port = str(31331 + index)
            command = [
                sys.executable,
                "-m",
                "drift.cli",
                "server",
                manifest.source.repository,
                "--model_manifest",
                str(MANIFEST),
                "--block_indices",
                f"{index*16}:{(index+1)*16}",
                "--device",
                "cpu",
                "--cache_dir",
                str(ROOT / "cache"),
                "--max_disk_space",
                "40GiB",
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
                "600",
                "--session_timeout",
                "3600",
                "--step_timeout",
                "600",
                "--ready_timeout",
                "300",
                "--balance_quality",
                "0",
                "--no_auto_relay",
                "--host_maddrs",
                "/ip4/127.0.0.1/tcp/" + port,
                "--announce_maddrs",
                "/ip4/127.0.0.1/tcp/" + port,
                "--health_state_path",
                str(directory / "health.json"),
                "--identity_path",
                str(directory / "identity.key"),
                "--initial_peers",
                *peers,
            ]
            workers.append(
                subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1"),
                )
            )
        write("reference-status.json", {"phase": "load-four-rpc-workers"})
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in workers):
                raise RuntimeError("A reference RPC worker exited during loading; inspect worker logs")
            health_paths = [ROOT / f"reference-worker-{i}/health.json" for i in range(4)]
            if all(p.exists() and json.loads(p.read_text()).get("worker_healthy") for p in health_paths):
                break
            time.sleep(5)
        else:
            raise TimeoutError("RPC workers did not finish loading in 30 minutes")
        remote = AutoDistributedModelForCausalLM.from_pretrained(
            manifest.source.repository,
            revision=manifest.source.revision,
            token=False,
            initial_peers=peers,
            dht_prefix=manifest.dht_prefix,
            manifest_digest=manifest.digest,
            manifest_execution_profile=manifest.runtime.to_dict(),
            torch_dtype=torch.bfloat16,
            artifact_verifier=verifier,
            request_timeout=300,
            connect_timeout=30,
            max_retries=2,
            update_period=5,
            use_server_to_server=True,
        ).eval()
        for index, (inputs, expected_steps, tokens) in enumerate(references):
            current, cache = inputs, None
            with torch.inference_mode(), remote.inference_session(max_length=64) as session:
                for step, expected in enumerate(expected_steps):
                    started = time.monotonic()
                    output = remote(input_ids=current, past_key_values=cache, use_cache=True, logits_to_keep=1)
                    cache = output.past_key_values
                    actual = output.logits[0, -1].float().cpu()
                    delta = (actual - expected).abs()
                    finite = bool(torch.isfinite(actual).all())
                    numeric = finite and bool(torch.allclose(actual, expected, atol=ATOL, rtol=RTOL))
                    greedy = int(actual.argmax()) == tokens[step]
                    check = {
                        "prompt_index": index,
                        "step": step,
                        "finite": finite,
                        "all_vocabulary_logits_within_tolerance": numeric,
                        "greedy_token_equal": greedy,
                        "reference_token": tokens[step],
                        "actual_token": int(actual.argmax()),
                        "max_absolute_error": float(delta.max()),
                        "mean_absolute_error": float(delta.mean()),
                        "seconds": time.monotonic() - started,
                    }
                    result["checks"].append(check)
                    write("reference-status.json", dict(check, phase="distributed-forward"))
                    current = torch.tensor([[tokens[step]]])
                route = [(s.span.start, s.span.end, str(s.span.peer_id)) for s in session._server_sessions]
                assert len(route) == 4 and route[0][0] == 0 and route[-1][1] == 64
        result["result"] = (
            "passed"
            if all(c["all_vocabulary_logits_within_tolerance"] and c["greedy_token_equal"] for c in result["checks"])
            and len(result["checks"]) == len(PROMPTS) * STEPS
            else "failed"
        )
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if remote is not None:
            try:
                _runtime_closer(remote)()
            except Exception as exc:
                result.update(result="failed", client_cleanup_error=str(exc))
        for process in workers:
            if process.poll() is None:
                process.terminate()
        for process in workers:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for log in logs:
            log.close()
        if dht is not None:
            dht.shutdown()
        write("reference-result.json", result)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        write("reference-error.json", {"error": f"{type(exc).__name__}: {exc}"})
        raise
