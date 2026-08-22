"""Run a one-machine TinyLlama private swarm smoke test.

This starts one local DHT peer that hosts all blocks for Maykeye/TinyLLama-v0,
connects a client through that peer's DHT address, and generates a few tokens.
It is intended for Windows/XPU bring-up but also works on CPU/CUDA with --device.

With ``--test-failover``, the script instead starts a bootstrap plus two complete
worker replicas, stops the selected worker inside an active generation session,
and verifies that replay on the surviving worker preserves exact token parity.
"""

from __future__ import annotations

import argparse
import faulthandler
import shutil
import tempfile
import time
from pathlib import Path

import torch
from hivemind import DHT
from hivemind.proto.runtime_pb2 import CompressionType
from hivemind.utils.timed_storage import MAX_DHT_TIME_DISCREPANCY_SECONDS
from transformers import AutoModelForCausalLM, AutoTokenizer

import drift
from drift import AutoDistributedModelForCausalLM
from drift.constants import DTYPE_MAP
from drift.data_structures import UID_DELIMITER, ModelInfo, ServerInfo, ServerState
from drift.model_manifest import ManifestArtifactVerifier, ModelManifest, resolve_manifest_loading
from drift.protocol_identity import NodeIdentity
from drift.server.block_utils import resolve_block_dtype
from drift.server.server import ModuleContainer
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.convert_block import QuantType
from drift.utils.dht import get_remote_module_infos
from drift.utils.hardware import normalize_device
from drift.utils.misc import get_size_in_bytes

MODEL = "Maykeye/TinyLLama-v0"
DHT_PREFIX = "_windows_xpu_tinyllama_v0_smoke"


def log(message: str) -> None:
    print(message, flush=True)


def parse_block_indices(value: str) -> list[int]:
    try:
        start_block, end_block = [int(index.strip()) for index in value.split(":")]
    except Exception as exc:
        raise ValueError("--block-indices must be start:end, e.g. 0:8") from exc
    return list(range(start_block, end_block))


def wait_for_dht_announcement(
    dht: DHT,
    dht_prefix: str,
    block_indices: list[int],
    timeout: float,
    *,
    min_replicas: int = 1,
    manifest_digest: str | None = None,
    manifest_execution_profile: dict | None = None,
) -> None:
    uids = [f"{dht_prefix}{UID_DELIMITER}{block_index}" for block_index in block_indices]
    deadline = time.time() + timeout
    while time.time() < deadline:
        module_infos = get_remote_module_infos(
            dht,
            uids,
            manifest_digest=manifest_digest,
            manifest_execution_profile=manifest_execution_profile,
            latest=True,
        )
        replicas = [len(module_info.servers) for module_info in module_infos]
        announced = sum(count >= min_replicas for count in replicas)
        log(f"announced_blocks={announced}/{len(uids)} replicas={replicas}")
        if announced == len(uids):
            return
        time.sleep(0.5)
    raise TimeoutError("hosted blocks were not announced in the local DHT")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="xpu")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--block-indices", default="0:8")
    parser.add_argument("--torch-dtype", default=None, choices=DTYPE_MAP.keys())
    parser.add_argument("--model-manifest", help="Run the smoke through a verified ModelManifest v1")
    parser.add_argument("--cache", default="contiguous", choices=["contiguous", "paged"])
    parser.add_argument("--page-size", type=int, default=16)
    parser.add_argument(
        "--test-failover",
        action="store_true",
        help="start two worker replicas, stop the selected worker during generation, and verify recovery",
    )
    parser.add_argument("--failover-tokens", type=int, default=8)
    parser.add_argument(
        "--skip-reference",
        action="store_true",
        help="skip the stock-model token parity check (faster, but does not verify numerical correctness)",
    )
    args = parser.parse_args()

    manifest = None
    artifact_verifier = None
    revision = None
    dht_prefix = DHT_PREFIX
    if args.model_manifest:
        manifest = ModelManifest.load(args.model_manifest)
        manifest.validate_runtime(drift.__version__)
        revision, dht_prefix = resolve_manifest_loading(
            manifest,
            model_name_or_path=MODEL,
            revision=None,
            dht_prefix=None,
        )
        if manifest.runtime.quantization != "none":
            raise ValueError("The TinyLlama smoke currently supports only manifest quantization='none'")
        if args.torch_dtype is None:
            args.torch_dtype = manifest.runtime.dtype
        elif args.torch_dtype != manifest.runtime.dtype:
            raise ValueError(
                f"--torch-dtype {args.torch_dtype!r} conflicts with manifest dtype {manifest.runtime.dtype!r}"
            )
        artifact_verifier = ManifestArtifactVerifier(manifest, MODEL, revision)
        config_source = artifact_verifier.ensure_startup_metadata(include_tokenizer=True)
    else:
        args.torch_dtype = args.torch_dtype or "bfloat16"
        config_source = MODEL

    faulthandler.dump_traceback_later(args.timeout, exit=True)

    device = normalize_device(torch.device(args.device))
    if device.type == "xpu":
        assert torch.xpu.is_available(), "XPU is not available"
        log(f"torch={torch.__version__}, xpu={torch.xpu.get_device_name(0)}")
    else:
        log(f"torch={torch.__version__}, device={device}")

    block_indices = parse_block_indices(args.block_indices)
    log(f"initializing local DHT for blocks={block_indices}")
    identity_dir = Path(tempfile.mkdtemp(prefix="drift-smoke-identities-")) if manifest is not None else None
    bootstrap_identity_path = identity_dir / "bootstrap.key" if identity_dir is not None else None
    dht = DHT(
        initial_peers=[],
        start=True,
        num_workers=len(block_indices),
        use_relay=False,
        use_auto_relay=False,
        client_mode=False,
        host_maddrs=["/ip4/127.0.0.1/tcp/0"],
        identity_path=str(bootstrap_identity_path) if bootstrap_identity_path is not None else None,
        tls=True,
    )
    containers = []
    worker_dhts = []

    try:
        peers = [str(addr) for addr in dht.get_visible_maddrs()]
        log(f"initial_peers={peers}")

        log("loading config")
        block_config = AutoDistributedConfig.from_pretrained(
            config_source,
            revision=None if manifest is not None else revision,
            local_files_only=manifest is not None,
        )
        if manifest is not None:
            manifest.validate_model_config(block_config)
        block_config._attn_implementation = "eager"
        torch_dtype = resolve_block_dtype(block_config, DTYPE_MAP[args.torch_dtype])
        log(f"torch_dtype={torch_dtype}")
        attn_cache_tokens = 128
        cache_values_per_block = 2 * block_config.hidden_size * attn_cache_tokens
        cache_values_per_block //= block_config.num_key_value_groups
        attn_cache_bytes = cache_values_per_block * get_size_in_bytes(torch_dtype) * len(block_indices)

        serving_dhts = [dht]
        serving_identities = [NodeIdentity.load(bootstrap_identity_path)] if bootstrap_identity_path else [None]
        if args.test_failover:
            serving_dhts = []
            serving_identities = []
            for replica_index in range(2):
                log(f"starting worker DHT replica={replica_index}")
                worker_identity_path = identity_dir / f"worker-{replica_index}.key" if identity_dir else None
                worker_dht = DHT(
                    initial_peers=peers,
                    start=True,
                    num_workers=len(block_indices),
                    use_relay=False,
                    use_auto_relay=False,
                    client_mode=False,
                    host_maddrs=["/ip4/127.0.0.1/tcp/0"],
                    identity_path=str(worker_identity_path) if worker_identity_path is not None else None,
                    tls=True,
                )
                worker_dhts.append(worker_dht)
                serving_dhts.append(worker_dht)
                serving_identities.append(NodeIdentity.load(worker_identity_path) if worker_identity_path else None)

        for replica_index, (serving_dht, serving_identity) in enumerate(zip(serving_dhts, serving_identities)):
            server_info = ServerInfo(
                state=ServerState.JOINING,
                throughput=1.0,
                manifest_digest=manifest.digest if manifest is not None else None,
                torch_dtype=str(torch_dtype).removeprefix("torch."),
                quant_type=QuantType.NONE.name.lower(),
                using_relay=False,
            )
            model_info = ModelInfo(num_blocks=block_config.num_hidden_layers, repository=MODEL)

            log(f"starting module container replica={replica_index}")
            container = ModuleContainer.create(
                dht=serving_dht,
                dht_prefix=dht_prefix,
                converted_model_name_or_path=MODEL,
                block_config=block_config,
                attn_cache_bytes=attn_cache_bytes,
                server_info=server_info,
                model_info=model_info,
                block_indices=block_indices,
                num_handlers=1,
                min_batch_size=1,
                max_batch_size=64,
                max_chunk_size_bytes=16 * 1024 * 1024,
                max_alloc_timeout=30,
                paged_cache=args.cache == "paged",
                page_size=args.page_size,
                inference_max_length=64,
                torch_dtype=torch_dtype,
                cache_dir=None,
                max_disk_space=None,
                device=device,
                compression=CompressionType.NONE,
                stats_report_interval=None,
                update_period=2 if args.test_failover else 5,
                expiration=max(10, MAX_DHT_TIME_DISCREPANCY_SECONDS),
                request_timeout=3 if args.test_failover else 60,
                session_timeout=60,
                step_timeout=30,
                prefetch_batches=1,
                sender_threads=1,
                revision=revision,
                token=None,
                model_manifest=manifest,
                protocol_identity=serving_identity,
                manifest_execution_profile=manifest.runtime.to_dict() if manifest is not None else None,
                quant_type=QuantType.NONE,
                tensor_parallel_devices=(device,),
                start=True,
            )
            containers.append(container)
            assert container.ready.wait(timeout=30), "module container did not become ready"

        wait_for_dht_announcement(
            dht,
            dht_prefix,
            block_indices,
            timeout=30,
            min_replicas=2 if args.test_failover else 1,
            manifest_digest=manifest.digest if manifest is not None else None,
            manifest_execution_profile=manifest.runtime.to_dict() if manifest is not None else None,
        )

        log("loading client")
        tokenizer = AutoTokenizer.from_pretrained(
            artifact_verifier.snapshot_root if artifact_verifier is not None else MODEL,
            revision=None if manifest is not None else revision,
            local_files_only=manifest is not None,
        )
        model_kwargs = dict(
            dht_prefix=dht_prefix,
            initial_peers=peers,
            revision=revision,
            manifest_digest=manifest.digest if manifest is not None else None,
            manifest_execution_profile=manifest.runtime.to_dict() if manifest is not None else None,
            torch_dtype=torch_dtype,
            request_timeout=3 if args.test_failover else 60,
            max_retries=3 if args.test_failover else None,
            min_backoff=0.1 if args.test_failover else 1,
            max_backoff=1 if args.test_failover else 60,
            update_period=1 if args.test_failover else 60,
        )
        if artifact_verifier is not None:
            model_kwargs["artifact_verifier"] = artifact_verifier
        model = AutoDistributedModelForCausalLM.from_pretrained(MODEL, **model_kwargs)

        log("generating")
        inputs = tokenizer("Hello", return_tensors="pt")["input_ids"]
        generated_tokens = args.failover_tokens if args.test_failover else 3
        if args.test_failover:
            if generated_tokens < 2:
                raise ValueError("--failover-tokens must be at least 2")
            with torch.inference_mode(), model.inference_session(
                max_length=inputs.shape[1] + generated_tokens
            ) as inference_session:
                first_outputs = model.generate(
                    inputs,
                    max_new_tokens=1,
                    min_new_tokens=1,
                    do_sample=False,
                )
                assert len(inference_session._server_sessions) == 1, (
                    "the failover smoke requires one full-range selected worker, got "
                    f"{[server_session.span for server_session in inference_session._server_sessions]}"
                )
                selected_peer = inference_session._server_sessions[0].span.peer_id
                selected_index = next(
                    index for index, worker_dht in enumerate(worker_dhts) if worker_dht.peer_id == selected_peer
                )
                log(f"interrupting selected worker replica={selected_index} peer={selected_peer}")
                recovery_started = time.monotonic()
                containers[selected_index].shutdown()
                containers[selected_index].join(timeout=10)
                worker_dhts[selected_index].shutdown()
                worker_dhts[selected_index].join()

                remaining_outputs = model.generate(
                    None,
                    max_new_tokens=generated_tokens - 1,
                    min_new_tokens=generated_tokens - 1,
                    do_sample=False,
                )
                recovery_seconds = time.monotonic() - recovery_started
                outputs = torch.cat([first_outputs, remaining_outputs], dim=1)
                log(f"failover_recovery_seconds={recovery_seconds:.3f}")
        else:
            with torch.inference_mode():
                outputs = model.generate(inputs, max_new_tokens=generated_tokens, do_sample=False)
        log(f"output_ids={outputs.tolist()}")
        log(f"decoded={tokenizer.decode(outputs[0])!r}")

        if not args.skip_reference:
            log("loading stock local model for token parity check")
            reference_model = AutoModelForCausalLM.from_pretrained(MODEL, revision=revision, dtype=torch_dtype).to(
                device
            )
            reference_model.eval()
            with torch.inference_mode():
                if args.test_failover:
                    reference_outputs = reference_model.generate(
                        inputs.to(device),
                        max_new_tokens=generated_tokens,
                        min_new_tokens=generated_tokens,
                        do_sample=False,
                    ).cpu()
                else:
                    reference_outputs = reference_model.generate(
                        inputs.to(device), max_new_tokens=generated_tokens, do_sample=False
                    ).cpu()
            if not torch.equal(outputs.cpu(), reference_outputs):
                raise AssertionError(
                    "distributed output differs from the stock model: "
                    f"distributed={outputs.tolist()}, reference={reference_outputs.tolist()}"
                )
            log(f"reference_output_ids={reference_outputs.tolist()}")
            log("distributed output matches the stock model exactly")

        log("tinyllama local swarm smoke ok")
    finally:
        log("shutting down")
        for container in reversed(containers):
            if container.is_alive():
                container.shutdown()
                container.join(timeout=10)
        for worker_dht in reversed(worker_dhts):
            if worker_dht.is_alive():
                worker_dht.shutdown()
                worker_dht.join()
        dht.shutdown()
        dht.join()
        if identity_dir is not None:
            shutil.rmtree(identity_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
