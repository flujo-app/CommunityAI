"""Run a one-machine TinyLlama private swarm smoke test.

This starts one local DHT peer that hosts all blocks for Maykeye/TinyLLama-v0,
connects a client through that peer's DHT address, and generates a few tokens.
It is intended for Windows/XPU bring-up but also works on CPU/CUDA with --device.
"""

from __future__ import annotations

import argparse
import faulthandler
import time

import torch
from hivemind import DHT
from hivemind.proto.runtime_pb2 import CompressionType
from hivemind.utils.timed_storage import MAX_DHT_TIME_DISCREPANCY_SECONDS
from transformers import AutoModelForCausalLM, AutoTokenizer

from drift import AutoDistributedModelForCausalLM
from drift.constants import DTYPE_MAP
from drift.data_structures import UID_DELIMITER, ModelInfo, ServerInfo, ServerState
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


def wait_for_dht_announcement(dht: DHT, block_indices: list[int], timeout: float) -> None:
    uids = [f"{DHT_PREFIX}{UID_DELIMITER}{block_index}" for block_index in block_indices]
    deadline = time.time() + timeout
    while time.time() < deadline:
        module_infos = get_remote_module_infos(dht, uids, latest=True)
        announced = sum(bool(module_info.servers) for module_info in module_infos)
        log(f"announced_blocks={announced}/{len(uids)}")
        if announced == len(uids):
            return
        time.sleep(1)
    raise TimeoutError("hosted blocks were not announced in the local DHT")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="xpu")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--block-indices", default="0:8")
    parser.add_argument("--torch-dtype", default="bfloat16", choices=DTYPE_MAP.keys())
    parser.add_argument("--cache", default="contiguous", choices=["contiguous", "paged"])
    parser.add_argument("--page-size", type=int, default=16)
    parser.add_argument(
        "--skip-reference",
        action="store_true",
        help="skip the stock-model token parity check (faster, but does not verify numerical correctness)",
    )
    args = parser.parse_args()

    faulthandler.dump_traceback_later(args.timeout, exit=True)

    device = normalize_device(torch.device(args.device))
    if device.type == "xpu":
        assert torch.xpu.is_available(), "XPU is not available"
        log(f"torch={torch.__version__}, xpu={torch.xpu.get_device_name(0)}")
    else:
        log(f"torch={torch.__version__}, device={device}")

    block_indices = parse_block_indices(args.block_indices)
    log(f"initializing local DHT for blocks={block_indices}")
    dht = DHT(
        initial_peers=[],
        start=True,
        num_workers=len(block_indices),
        use_relay=False,
        use_auto_relay=False,
        client_mode=False,
        host_maddrs=["/ip4/127.0.0.1/tcp/0"],
    )
    container = None

    try:
        peers = [str(addr) for addr in dht.get_visible_maddrs()]
        log(f"initial_peers={peers}")

        log("loading config")
        block_config = AutoDistributedConfig.from_pretrained(MODEL)
        block_config._attn_implementation = "eager"
        torch_dtype = resolve_block_dtype(block_config, DTYPE_MAP[args.torch_dtype])
        log(f"torch_dtype={torch_dtype}")
        attn_cache_tokens = 128
        cache_values_per_block = 2 * block_config.hidden_size * attn_cache_tokens
        cache_values_per_block //= block_config.num_key_value_groups
        attn_cache_bytes = cache_values_per_block * get_size_in_bytes(torch_dtype) * len(block_indices)

        server_info = ServerInfo(
            state=ServerState.JOINING,
            throughput=1.0,
            torch_dtype="bfloat16",
            quant_type=QuantType.NONE.name.lower(),
            using_relay=False,
        )
        model_info = ModelInfo(num_blocks=block_config.num_hidden_layers, repository=MODEL)

        log("starting module container")
        container = ModuleContainer.create(
            dht=dht,
            dht_prefix=DHT_PREFIX,
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
            update_period=5,
            expiration=max(10, MAX_DHT_TIME_DISCREPANCY_SECONDS),
            request_timeout=60,
            session_timeout=60,
            step_timeout=30,
            prefetch_batches=1,
            sender_threads=1,
            revision=None,
            token=None,
            quant_type=QuantType.NONE,
            tensor_parallel_devices=(device,),
            start=True,
        )
        assert container.ready.wait(timeout=30), "module container did not become ready"

        wait_for_dht_announcement(dht, block_indices, timeout=30)

        log("loading client")
        tokenizer = AutoTokenizer.from_pretrained(MODEL)
        model = AutoDistributedModelForCausalLM.from_pretrained(
            MODEL,
            dht_prefix=DHT_PREFIX,
            initial_peers=peers,
            torch_dtype=torch_dtype,
            request_timeout=60,
        )

        log("generating")
        inputs = tokenizer("Hello", return_tensors="pt")["input_ids"]
        with torch.inference_mode():
            outputs = model.generate(inputs, max_new_tokens=3, do_sample=False)
        log(f"output_ids={outputs.tolist()}")
        log(f"decoded={tokenizer.decode(outputs[0])!r}")

        if not args.skip_reference:
            log("loading stock local model for token parity check")
            reference_model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=torch_dtype).to(device)
            reference_model.eval()
            with torch.inference_mode():
                reference_outputs = reference_model.generate(inputs.to(device), max_new_tokens=3, do_sample=False).cpu()
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
        if container is not None:
            container.shutdown()
            container.join(timeout=10)
        dht.shutdown()
        dht.join()


if __name__ == "__main__":
    main()
