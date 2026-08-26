from __future__ import annotations

import gc
import math
import multiprocessing as mp
import os
import random
import sys
import threading
import time
from typing import Dict, List, Optional, Sequence, Union

import torch
from hivemind import DHT
from hivemind.moe.server.layers import add_custom_models_from_file
from hivemind.moe.server.runtime import Runtime
from hivemind.p2p import PeerID
from hivemind.proto.runtime_pb2 import CompressionType
from hivemind.utils.logging import get_logger
from hivemind.utils.tensor_descr import BatchTensorDescriptor
from hivemind.utils.timed_storage import MAX_DHT_TIME_DISCREPANCY_SECONDS, get_dht_time
from transformers import PretrainedConfig

import drift
from drift.constants import DTYPE_MAP
from drift.data_structures import CHAIN_DELIMITER, UID_DELIMITER, ModelInfo, ServerInfo, ServerState, parse_uid
from drift.model_manifest import ManifestArtifactVerifier, ModelManifest
from drift.protocol_identity import (
    MAX_SIGNED_RECORD_TTL_SECONDS,
    NodeIdentity,
    ProtocolSecurityError,
    ReplayGuard,
    RevocationStore,
    create_worker_announcement,
)
from drift.server import block_selection
from drift.server.backend import TransformerBackend, merge_inference_pools_inplace
from drift.server.block_utils import get_block_size, resolve_block_dtype
from drift.server.from_pretrained import load_pretrained_block
from drift.server.handler import TransformerConnectionHandler
from drift.server.memory_cache import MemoryCache
from drift.server.reachability import ReachabilityProtocol, check_direct_reachability
from drift.server.throughput import get_dtype_name, get_server_throughput
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.convert_block import QuantType, check_device_balance, convert_block
from drift.utils.dht import declare_active_modules, get_remote_module_infos
from drift.utils.hardware import (
    auto_detect_device,
    empty_device_cache,
    get_device_total_memory,
    get_memory_stats,
    is_accelerator,
    normalize_device,
    supports_dtype,
)
from drift.utils.kv_cache import StandardGQACache
from drift.utils.misc import format_all_thread_stacks, get_size_in_bytes
from drift.utils.ping import PingAggregator
from drift.utils.random import sample_up_to
from drift.utils.version import get_compatible_model_repo

logger = get_logger(__name__)


def _probe_quantization(quant_type: QuantType, device: torch.device) -> Optional[str]:
    """Return None if ``quant_type`` can actually run on ``device``, else a short reason why not.

    bitsandbytes imports cleanly but ships without a working native library on some platforms (e.g.
    ROCm on Windows); otherwise the failure only surfaces deep inside block loading, after the 5 GB of
    weights are already downloaded. A tiny quantization exercises the shared native library that both
    the nf4 and int8 paths depend on, so we can catch the problem before loading anything.
    """
    if quant_type == QuantType.NONE:
        return None
    try:
        import bitsandbytes as bnb

        bnb.functional.quantize_4bit(torch.zeros(64, 64, dtype=torch.bfloat16, device=device))
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


class Server:
    """
    Runs ModuleContainer, periodically checks that the network is balanced,
    restarts the ModuleContainer with other layers if the imbalance is significant
    """

    def __init__(
        self,
        *,
        initial_peers: List[str],
        dht_prefix: Optional[str],
        converted_model_name_or_path: str,
        public_name: Optional[str] = None,
        throughput: Union[float, str],
        num_blocks: Optional[int] = None,
        block_indices: Optional[str] = None,
        num_handlers: int = 1,
        inference_max_length: Optional[int] = None,
        min_batch_size: int = 1,
        max_batch_size: Optional[int] = None,
        max_chunk_size_bytes: int = 256 * 1024 * 1024,
        max_alloc_timeout: float = 600,
        attn_cache_tokens: Optional[int] = None,
        cache: str = "contiguous",
        page_size: int = 16,
        torch_dtype: str = "auto",
        attn_implementation: str = "auto",
        revision: Optional[str] = None,
        model_manifest: Optional[ModelManifest] = None,
        revocation_files: Sequence[str] = (),
        cache_dir: Optional[str] = None,
        max_disk_space: Optional[int] = None,
        device: Optional[Union[str, torch.device]] = None,
        compression=CompressionType.NONE,
        stats_report_interval: Optional[int] = None,
        custom_module_path=None,
        update_period: float = 60,
        expiration: Optional[float] = None,
        request_timeout: float = 3 * 60,
        session_timeout: float = 30 * 60,
        step_timeout: float = 5 * 60,
        prefetch_batches: int = 1,
        sender_threads: int = 1,
        balance_quality: float = 0.75,
        mean_balance_check_period: float = 120,
        mean_block_selection_delay: float = 5,
        ready_timeout: float = 120,
        token: Optional[Union[str, bool]] = None,
        quant_type: Optional[QuantType] = None,
        tensor_parallel_devices: Optional[Sequence[torch.device]] = None,
        reachable_via_relay: Optional[bool] = None,
        use_relay: bool = True,
        use_auto_relay: bool = True,
        adapters: Sequence[str] = (),
        **kwargs,
    ):
        """Create a server with one or more bloom blocks. See run_server.py for documentation."""

        converted_model_name_or_path = get_compatible_model_repo(converted_model_name_or_path)
        self.converted_model_name_or_path = converted_model_name_or_path

        self.num_handlers = num_handlers
        self.compression = compression
        self.stats_report_interval, self.update_period = stats_report_interval, update_period
        self.prefetch_batches, self.sender_threads = prefetch_batches, sender_threads
        self.revision, self.token = revision, token

        if custom_module_path is not None:
            add_custom_models_from_file(custom_module_path)

        artifact_verifier = None
        if model_manifest is not None:
            model_manifest.validate_runtime(drift.__version__)
            artifact_verifier = ManifestArtifactVerifier(
                model_manifest,
                repository=converted_model_name_or_path,
                revision=revision,
                token=token,
                cache_dir=cache_dir,
                max_disk_space=max_disk_space,
            )
            config_source = artifact_verifier.ensure_startup_metadata()
        else:
            config_source = converted_model_name_or_path

        self.block_config = AutoDistributedConfig.from_pretrained(
            config_source,
            token=token,
            revision=None if artifact_verifier is not None else revision,
            local_files_only=artifact_verifier is not None,
        )
        if model_manifest is not None:
            model_manifest.validate_model_config(self.block_config)
        self.model_manifest = model_manifest
        self.manifest_execution_profile = model_manifest.runtime.to_dict() if model_manifest is not None else None
        self.revocations = RevocationStore.from_files(revocation_files)
        self.announcement_replay_guard = ReplayGuard()

        identity_path = kwargs.get("identity_path")
        self.protocol_identity = None
        if model_manifest is not None:
            if not identity_path:
                raise ProtocolSecurityError(
                    "--identity_path is required with --model_manifest so public announcements have a stable signer"
                )
            if kwargs.get("tls", True) is not True:
                raise ProtocolSecurityError("Manifested public swarms require libp2p TLS 1.3 transport security")
            self.protocol_identity = NodeIdentity.ensure(identity_path)
            self.revocations.require_active(self.protocol_identity.key_id)
            kwargs["tls"] = True

        # "auto" leaves _attn_implementation unset so each block picks its own correct default
        # (see drift.utils.misc.default_attn_implementation); an explicit choice forces every block.
        if attn_implementation != "auto":
            self.block_config._attn_implementation = attn_implementation

        if dht_prefix is None:
            dht_prefix = self.block_config.dht_prefix
        if model_manifest is not None and dht_prefix != model_manifest.dht_prefix:
            raise ProtocolSecurityError(
                f"Manifested server DHT prefix must be content-derived as {model_manifest.dht_prefix!r}"
            )
        assert UID_DELIMITER not in dht_prefix and CHAIN_DELIMITER not in dht_prefix, (
            f"DHT prefix should not contain '{UID_DELIMITER}' or '{CHAIN_DELIMITER}'. "
            f"Please specify another --dht_prefix manually when starting a server"
        )
        self.dht_prefix = dht_prefix

        if expiration is None:
            expiration = max(2 * update_period, MAX_DHT_TIME_DISCREPANCY_SECONDS)
        if model_manifest is not None and (
            not math.isfinite(expiration) or not 0 < expiration <= MAX_SIGNED_RECORD_TTL_SECONDS
        ):
            raise ProtocolSecurityError(
                f"Manifested announcement lifetime must be finite, positive, and at most "
                f"{MAX_SIGNED_RECORD_TTL_SECONDS} seconds"
            )
        self.expiration = expiration

        self.request_timeout = request_timeout
        self.session_timeout, self.step_timeout = session_timeout, step_timeout

        self.module_uids = [
            f"{self.dht_prefix}{UID_DELIMITER}{block_index}"
            for block_index in range(self.block_config.num_hidden_layers)
        ]

        if reachable_via_relay is None:
            is_reachable = check_direct_reachability(initial_peers=initial_peers, use_relay=False, **kwargs)
            reachable_via_relay = is_reachable is False  # if can't check reachability (returns None), run a full peer
            logger.info(f"This server is accessible {'via relays' if reachable_via_relay else 'directly'}")
        self.dht = DHT(
            initial_peers=initial_peers,
            start=True,
            num_workers=self.block_config.num_hidden_layers,
            use_relay=use_relay,
            use_auto_relay=use_auto_relay,
            client_mode=reachable_via_relay,
            **kwargs,
        )
        if self.protocol_identity is not None and self.dht.peer_id != self.protocol_identity.peer_id:
            raise ProtocolSecurityError("The running libp2p PeerID does not match the announcement signing identity")
        self.reachability_protocol = ReachabilityProtocol.attach_to_dht(self.dht) if not reachable_via_relay else None

        visible_maddrs_str = [str(a) for a in self.dht.get_visible_maddrs()]
        if initial_peers:
            logger.info(f"Connecting to a swarm, initial peers: {initial_peers}")
        else:
            logger.info("Starting a new swarm")
        logger.info(f"Running a server on {visible_maddrs_str}")

        if device is None:
            device = auto_detect_device()
        device = normalize_device(torch.device(device))
        self.device = device

        torch_dtype = resolve_block_dtype(self.block_config, DTYPE_MAP[torch_dtype])
        dtype_error = supports_dtype(device, torch_dtype)
        if dtype_error is not None:
            if device.type == "mps" and torch_dtype == torch.bfloat16:
                logger.warning(f"{dtype_error}, using float16 instead")
                torch_dtype = torch.float16
            else:
                raise ValueError(f"{dtype_error}. Please use a different --torch_dtype")
        self.torch_dtype = torch_dtype

        if tensor_parallel_devices is None:
            tensor_parallel_devices = (device,)
        self.tensor_parallel_devices = tuple(map(torch.device, tensor_parallel_devices))
        if len(self.tensor_parallel_devices) > 1:
            logger.info(f"Model weights will be split between {', '.join(tensor_parallel_devices)}")
            check_device_balance(self.tensor_parallel_devices)

        explicitly_requested_quant = quant_type is not None
        if quant_type is None:
            quant_type = QuantType.NF4 if device.type == "cuda" else QuantType.NONE
        # Fail fast (or fall back) if the chosen quantization can't run here, rather than crashing deep
        # inside block loading after the weights are downloaded (ROCm/Windows ships a broken bitsandbytes).
        quant_error = _probe_quantization(quant_type, device)
        if quant_error is not None:
            if explicitly_requested_quant:
                raise RuntimeError(
                    f"--quant_type {quant_type.name.lower()} was requested but cannot run on this device "
                    f"({quant_error}). Install a working bitsandbytes build or serve with --quant_type none."
                )
            logger.warning(
                f"Default {quant_type.name.lower()} quantization is unavailable on this device ({quant_error}); "
                f"falling back to uncompressed weights (--quant_type none)."
            )
            quant_type = QuantType.NONE
        self.quant_type = quant_type
        logger.info(f"Model weights are loaded in {get_dtype_name(torch_dtype, quant_type)} format")

        is_multiquery_attn = self.block_config.num_key_value_groups > 1
        if max_batch_size is None:
            max_batch_size = 8192 if is_multiquery_attn else 2048
        if inference_max_length is None:
            inference_max_length = 8192 if is_multiquery_attn else 2048
        self.min_batch_size, self.max_batch_size = min_batch_size, max_batch_size
        self.inference_max_length = inference_max_length
        self.max_chunk_size_bytes = max_chunk_size_bytes
        self.max_alloc_timeout = max_alloc_timeout

        assert cache in ("contiguous", "paged"), f"--cache must be 'contiguous' or 'paged', got {cache!r}"
        self.paged_cache = cache == "paged"
        self.page_size = page_size

        # For attention cache in GPU or RAM
        if attn_cache_tokens is None:
            attn_cache_tokens = 16384 if is_multiquery_attn else 4096
        cache_strategy = getattr(self.block_config, "kv_cache_strategy", StandardGQACache)
        cache_bytes_by_block = [
            cache_strategy.estimate_cache_bytes(
                self.block_config,
                attn_cache_tokens,
                dtype=self.torch_dtype,
                block_index=block_index,
            )
            for block_index in range(self.block_config.num_hidden_layers)
        ]
        self._cache_bytes_per_block = max(cache_bytes_by_block)

        # For disk cache
        self.cache_dir = cache_dir
        self.max_disk_space = max_disk_space
        self.adapters = adapters

        assert num_blocks is None or block_indices is None, "Please specify num_blocks or block_indices, not both"
        if num_blocks is None and block_indices is None:
            num_blocks = self._choose_num_blocks()
        if num_blocks is not None:
            num_blocks = min(num_blocks, self.block_config.num_hidden_layers)
        if block_indices is not None:
            try:
                start_block, end_block = [int(index.strip()) for index in block_indices.split(":")]
            except Exception as e:
                raise ValueError(f"Failed to parse `--block_indices {block_indices}`, must be start:end (e.g. 0:18)")
            block_indices = range(start_block, end_block)
            num_blocks = len(block_indices)
        self.strict_block_indices, self.num_blocks = block_indices, num_blocks

        gib = 1024**3
        self.attn_cache_bytes = (
            sum(cache_bytes_by_block[block_index] for block_index in block_indices)
            if block_indices is not None
            else self._cache_bytes_per_block * num_blocks
        )
        logger.info(f"Attention cache for all blocks will consume up to {self.attn_cache_bytes / gib:.2f} GiB")

        assert isinstance(throughput, float) or throughput in ["auto", "eval", "dry_run"]
        if throughput in ["auto", "eval", "dry_run"]:
            force_eval = throughput in ["eval", "dry_run"]
            throughput_info = get_server_throughput(
                converted_model_name_or_path,
                self.block_config,
                device,
                torch_dtype,
                num_blocks=num_blocks,
                quant_type=quant_type,
                tensor_parallel_devices=self.tensor_parallel_devices,
                reachable_via_relay=reachable_via_relay,
                force_eval=force_eval,
                cache_dir=cache_dir,
            )
            if throughput == "dry_run":
                logger.info("Finished estimating throughput, exiting")
                sys.exit(0)
        else:
            throughput_info = {"throughput": throughput}
        self.server_info = ServerInfo(
            state=ServerState.JOINING,
            public_name=public_name,
            version=drift.__version__,
            manifest_digest=model_manifest.digest if model_manifest is not None else None,
            adapters=tuple(adapters),
            torch_dtype=str(torch_dtype).replace("torch.", ""),
            quant_type=quant_type.name.lower(),
            using_relay=reachable_via_relay,
            **throughput_info,
        )
        self._warn_on_swarm_profile_mismatch()

        self.model_info = ModelInfo(num_blocks=self.block_config.num_hidden_layers)
        if not os.path.isdir(converted_model_name_or_path):
            self.model_info.repository = "https://huggingface.co/" + converted_model_name_or_path

        self.balance_quality = balance_quality
        self.mean_balance_check_period = mean_balance_check_period
        self.mean_block_selection_delay = mean_block_selection_delay
        self.ready_timeout = ready_timeout

        self.module_container = None
        self.stop = threading.Event()

    def _warn_on_swarm_profile_mismatch(self) -> None:
        """Best-effort: warn if peers already serving this model use a different dtype/quant.

        torch_dtype and weight quantization are per-server serving choices already advertised in each
        ServerInfo. A swarm that mixes them still runs, but stitches together numerically inconsistent
        blocks, so results depend on which servers a route lands on. This only warns -- it never blocks
        joining, since heterogeneous swarms remain allowed.
        """
        ours = (self.server_info.torch_dtype, self.server_info.quant_type)
        try:
            module_infos = get_remote_module_infos(
                self.dht,
                self.module_uids,
                manifest_digest=self.server_info.manifest_digest,
                manifest_execution_profile=self.manifest_execution_profile,
                revocations=self.revocations,
                replay_guard=self.announcement_replay_guard,
                latest=True,
            )
            others = set()
            for module_info in module_infos:
                for peer_id, server_info in module_info.servers.items():
                    if peer_id == self.dht.peer_id or server_info.quant_type is None:
                        continue  # skip ourselves and older servers that don't advertise a quant_type
                    others.add((server_info.torch_dtype, server_info.quant_type))
        except Exception as e:
            logger.debug(f"Skipping swarm dtype/quant consistency check: {e}")
            return

        mismatches = sorted(f"{dtype}/{quant}" for dtype, quant in others if (dtype, quant) != ours)
        if mismatches:
            logger.warning(
                f"This server serves {ours[0]}/{ours[1]} (dtype/quant), but the swarm already has servers "
                f"running {', '.join(mismatches)}. Mixing precision or quantization across a swarm yields "
                f"inconsistent results depending on the route; consider matching --torch_dtype and "
                f"--quant_type across all servers."
            )

    def _choose_num_blocks(self) -> int:
        assert is_accelerator(self.device), (
            "GPU is not available. If you want to run a CPU-only server, please specify --num_blocks. "
            "CPU-only servers are much slower, so this must be requested explicitly."
        )
        num_devices = len(self.tensor_parallel_devices) if self.tensor_parallel_devices else 1

        if num_devices > 1:
            assert self.device.type == "cuda", f"Tensor parallelism is not supported on {self.device.type.upper()}"
            memory_per_device = tuple(get_device_total_memory(device) for device in self.tensor_parallel_devices)
            total_memory = min(memory_per_device) * num_devices
            if max(memory_per_device) / min(memory_per_device) > 1.5:
                raise ValueError(
                    "GPU devices have highly uneven memory, which makes tensor parallelism inefficient. "
                    "Please launch individual servers on each GPU or set --num_blocks manually to "
                    "override this exception."
                )
        else:
            total_memory = get_device_total_memory(self.device)

        gib = 1024**3
        # Estimate of GPU memory used in rpc_backward (2 GiB for BLOOM, proportional for other models)
        autograd_memory = 2 * gib * num_devices / 14336 * self.block_config.hidden_size

        block_size = get_block_size(self.block_config, "memory", dtype=self.torch_dtype, quant_type=self.quant_type)
        total_memory_per_block = block_size + self._cache_bytes_per_block
        if self.adapters:
            # Delay import of drift.utils.peft to avoid unnecessary import of bitsandbytes
            from drift.utils.peft import estimate_adapter_memory_per_block

            total_memory_per_block += estimate_adapter_memory_per_block(
                self.block_config,
                self.torch_dtype,
                self.adapters,
                token=self.token,
                cache_dir=self.cache_dir,
                max_disk_space=self.max_disk_space,
            )

        num_blocks = math.floor((total_memory - autograd_memory) / total_memory_per_block)
        assert num_blocks >= 1, "Your GPU does not have enough memory to serve at least one block"

        num_blocks = min(num_blocks, self.block_config.num_hidden_layers)
        logger.info(
            f"Server will fill your GPU memory with {num_blocks} transformer blocks. "
            f"If you want to leave some free GPU memory, please specify a lesser --num_blocks manually"
        )
        return num_blocks

    def run(self):
        while True:
            block_indices = self._choose_blocks()
            self.module_container = ModuleContainer.create(
                dht=self.dht,
                dht_prefix=self.dht_prefix,
                converted_model_name_or_path=self.converted_model_name_or_path,
                block_config=self.block_config,
                attn_cache_bytes=self.attn_cache_bytes,
                server_info=self.server_info,
                model_info=self.model_info,
                block_indices=block_indices,
                num_handlers=self.num_handlers,
                min_batch_size=self.min_batch_size,
                max_batch_size=self.max_batch_size,
                max_chunk_size_bytes=self.max_chunk_size_bytes,
                max_alloc_timeout=self.max_alloc_timeout,
                paged_cache=self.paged_cache,
                page_size=self.page_size,
                inference_max_length=self.inference_max_length,
                torch_dtype=self.torch_dtype,
                cache_dir=self.cache_dir,
                max_disk_space=self.max_disk_space,
                device=self.device,
                compression=self.compression,
                stats_report_interval=self.stats_report_interval,
                update_period=self.update_period,
                expiration=self.expiration,
                request_timeout=self.request_timeout,
                session_timeout=self.session_timeout,
                step_timeout=self.step_timeout,
                prefetch_batches=self.prefetch_batches,
                sender_threads=self.sender_threads,
                revision=self.revision,
                token=self.token,
                model_manifest=self.model_manifest,
                protocol_identity=self.protocol_identity,
                manifest_execution_profile=self.manifest_execution_profile,
                revocations=self.revocations,
                replay_guard=self.announcement_replay_guard,
                quant_type=self.quant_type,
                tensor_parallel_devices=self.tensor_parallel_devices,
                ready_timeout=self.ready_timeout,
                start=True,
            )
            try:
                self.module_container.ready.wait()

                while True:
                    timeout = random.random() * 2 * self.mean_balance_check_period
                    if self.stop.wait(timeout):
                        return

                    if not self.module_container.is_healthy():
                        logger.warning("One of subprocesses crashed, restarting the server")
                        break

                    if self._should_choose_other_blocks():
                        logger.info("Swarm is imbalanced, server will load other blocks")
                        break  # Stop serving this set of modules
            finally:
                self.module_container.shutdown()

            self._clean_memory_and_fds()

    def _clean_memory_and_fds(self):
        self.module_container = None
        gc.collect()  # In particular, this closes unused file descriptors

        if is_accelerator(self.device):
            empty_device_cache(self.device)

            memory_stats = get_memory_stats(self.device)
            if memory_stats is not None:
                allocated_vram, reserved_vram = memory_stats
                gib = 1024**3
                logger.info(
                    f"Cleaning up, left {allocated_vram / gib:.1f} GiB allocated memory, "
                    f"{reserved_vram / gib:.1f} GiB reserved memory"
                )

    def _choose_blocks(self) -> List[int]:
        if self.strict_block_indices is not None:
            return self.strict_block_indices

        # If multiple servers (e.g., launched on the same machine by a script) get to this line at the same time,
        # this delay decreases the probability of a race condition while choosing the best blocks to serve.
        time.sleep(random.random() * 2 * self.mean_block_selection_delay)
        module_infos = get_remote_module_infos(
            self.dht,
            self.module_uids,
            manifest_digest=self.server_info.manifest_digest,
            manifest_execution_profile=self.manifest_execution_profile,
            revocations=self.revocations,
            replay_guard=self.announcement_replay_guard,
            latest=True,
        )
        return block_selection.choose_best_blocks(self.num_blocks, module_infos)

    def _should_choose_other_blocks(self) -> bool:
        if self.strict_block_indices is not None:
            return False

        module_infos = get_remote_module_infos(
            self.dht,
            self.module_uids,
            manifest_digest=self.server_info.manifest_digest,
            manifest_execution_profile=self.manifest_execution_profile,
            revocations=self.revocations,
            replay_guard=self.announcement_replay_guard,
            latest=True,
        )
        return block_selection.should_choose_other_blocks(self.dht.peer_id, module_infos, self.balance_quality)

    def shutdown(self, timeout: Optional[float] = 5):
        self.stop.set()
        if self.module_container is not None and self.module_container.is_alive():
            self.module_container.join(timeout)

        if self.reachability_protocol is not None:
            self.reachability_protocol.shutdown()
        self.dht.shutdown()
        self.dht.join()


class ModuleContainer(threading.Thread):
    """Serves a set of specific Bloom layers for inference, forward, and backward. Announces itself over the DHT."""

    # noinspection PyMethodOverriding
    @classmethod
    def create(
        cls,
        *,
        dht: DHT,
        dht_prefix: str,
        converted_model_name_or_path: str,
        block_config: PretrainedConfig,
        attn_cache_bytes: int,
        server_info: ServerInfo,
        model_info: ModelInfo,
        block_indices: List[int],
        min_batch_size: int,
        max_batch_size: int,
        max_chunk_size_bytes: int,
        max_alloc_timeout: float,
        paged_cache: bool = False,
        page_size: int = 16,
        torch_dtype: torch.dtype,
        cache_dir: str,
        max_disk_space: int,
        device: Union[str, torch.device],
        compression: CompressionType,
        update_period: float,
        expiration: Optional[float],
        revision: Optional[str],
        token: Optional[Union[str, bool]],
        model_manifest: Optional[ModelManifest] = None,
        protocol_identity: Optional[NodeIdentity] = None,
        manifest_execution_profile: Optional[Dict[str, object]] = None,
        revocations: Optional[RevocationStore] = None,
        replay_guard: Optional[ReplayGuard] = None,
        quant_type: QuantType,
        tensor_parallel_devices: Sequence[torch.device],
        **kwargs,
    ) -> ModuleContainer:
        module_uids = [f"{dht_prefix}{UID_DELIMITER}{block_index}" for block_index in block_indices]
        memory_cache = MemoryCache(attn_cache_bytes, max_alloc_timeout, paged=paged_cache, page_size=page_size)

        server_info.state = ServerState.JOINING
        dht_announcer = ModuleAnnouncerThread(
            module_uids,
            dht,
            server_info,
            model_info,
            block_config=block_config,
            memory_cache=memory_cache,
            update_period=update_period,
            expiration=expiration,
            protocol_identity=protocol_identity,
            manifest_execution_profile=manifest_execution_profile,
            revocations=revocations,
            replay_guard=replay_guard,
            daemon=True,
        )
        dht_announcer.start()
        logger.info(f"Announced that blocks {block_indices} are joining")

        assert len(tensor_parallel_devices) >= 1 and all(isinstance(d, torch.device) for d in tensor_parallel_devices)

        blocks = {}
        try:
            artifact_verifier = (
                ManifestArtifactVerifier(
                    model_manifest,
                    repository=converted_model_name_or_path,
                    revision=revision,
                    token=token,
                    cache_dir=cache_dir,
                    max_disk_space=max_disk_space,
                )
                if model_manifest is not None
                else None
            )
            for module_uid, block_index in zip(module_uids, block_indices):
                block = load_pretrained_block(
                    converted_model_name_or_path,
                    block_index,
                    config=block_config,
                    torch_dtype=torch_dtype,
                    revision=revision,
                    token=token,
                    cache_dir=cache_dir,
                    max_disk_space=max_disk_space,
                    artifact_verifier=artifact_verifier,
                )
                block = convert_block(
                    block,
                    block_index,
                    block_config,
                    tensor_parallel_devices,
                    device,
                    quant_type,
                    adapters=server_info.adapters,
                    freeze=True,
                    token=token,
                    cache_dir=cache_dir,
                    max_disk_space=max_disk_space,
                )
                blocks[module_uid] = TransformerBackend(
                    module_uid,
                    block,
                    config=block_config,
                    memory_cache=memory_cache,
                    backend_dtype=torch_dtype,
                    max_chunk_size_bytes=max_chunk_size_bytes,
                    args_schema=(
                        BatchTensorDescriptor(
                            1, 2048, block_config.hidden_size, dtype=torch_dtype, compression=compression
                        ),
                    ),
                    kwargs_schema={},
                    outputs_schema=(
                        BatchTensorDescriptor(
                            1, 2048, block_config.hidden_size, dtype=torch_dtype, compression=compression
                        ),
                    ),
                    min_batch_size=min_batch_size,
                    max_batch_size=max_batch_size,
                )

            logger.info(f"Initialized backends for {len(blocks)} blocks, merging inference pools")
            merge_inference_pools_inplace(blocks)
        except:
            logger.debug("Shutting down backends")
            for backend in blocks.values():
                backend.shutdown()

            dht_announcer.announce(ServerState.OFFLINE)
            logger.info(f"Announced that blocks {module_uids} are offline")
            raise

        return cls(
            dht,
            dht_prefix,
            blocks,
            dht_announcer=dht_announcer,
            server_info=server_info,
            protocol_identity=protocol_identity,
            update_period=update_period,
            expiration=expiration,
            **kwargs,
        )

    def __init__(
        self,
        dht: DHT,
        dht_prefix: str,
        module_backends: Dict[str, TransformerBackend],
        *,
        inference_max_length: int,
        num_handlers: int,
        dht_announcer: ModuleAnnouncerThread,
        server_info: ServerInfo,
        protocol_identity: Optional[NodeIdentity] = None,
        update_period: float,
        expiration: Optional[float] = None,
        request_timeout: float,
        session_timeout: float,
        step_timeout: float,
        ready_timeout: float = 120,
        start: bool,
        **kwargs,
    ):
        # Daemon, so that a runtime wedged during startup cannot keep the interpreter alive after a
        # readiness timeout aborts the process; graceful teardown always goes through shutdown().
        super().__init__(daemon=True)
        self.ready_timeout = ready_timeout

        self.dht, self.module_backends = dht, module_backends
        self.server_info, self.update_period, self.expiration = server_info, update_period, expiration

        handler_event_queues = [mp.Queue() for _ in range(num_handlers)]
        self.conn_handlers = [
            TransformerConnectionHandler(
                dht,
                self.module_backends,
                adapters=server_info.adapters,
                dht_prefix=dht_prefix,
                handler_event_queues=handler_event_queues,
                handler_index=i,
                inference_max_length=inference_max_length,
                request_timeout=request_timeout,
                session_timeout=session_timeout,
                step_timeout=step_timeout,
                manifest_digest=server_info.manifest_digest,
                identity_key_id=protocol_identity.key_id if protocol_identity is not None else None,
                quant_type=QuantType[server_info.quant_type.upper()],
            )
            for i in range(num_handlers)
        ]

        self.runtime = RuntimeWithDeduplicatedPools(self.module_backends, device=None, **kwargs)
        # note: We set device=None in runtime to avoid moving all modules to device 0 in runtime.run(). tensor_parallel has already moved it as needed.

        dht_announcer.announce(ServerState.ONLINE)
        self.dht_announcer = dht_announcer

        if start:
            self.run_in_background(await_ready=True)

    def run(self):
        """
        Runs ModuleContainer in the current thread. Initializes dht if necessary, starts connection handlers,
        runs Runtime (self.runtime) to process incoming requests.
        """
        logger.info(f"Registering {len(self.conn_handlers)} connection handler(s) with the p2p daemon")
        for i, handler in enumerate(self.conn_handlers):
            try:
                handler.run_in_background(timeout=self.ready_timeout)
            except TimeoutError:
                # The historical wedge point: the handler drives the DHT's p2p daemon during
                # add_p2p_handlers, so a daemon/event-loop deadlock parks it here forever.
                logger.error(
                    f"Connection handler {i} did not become ready within {self.ready_timeout} seconds. "
                    f"Thread stacks:\n{format_all_thread_stacks()}"
                )
                raise

        logger.info("Connection handlers are ready, starting the runtime")
        self.runtime.run()

    def run_in_background(self, await_ready=True, timeout=None):
        """
        Starts ModuleContainer in a background thread. if await_ready, this method will wait until the container
        is ready to process incoming requests or for :timeout: seconds max (default: self.ready_timeout).
        On timeout, dumps every thread's stack (to localize the stuck phase) and raises instead of hanging.
        """
        self.start()
        if await_ready:
            timeout = timeout if timeout is not None else self.ready_timeout
            if not self.ready.wait(timeout=timeout):
                logger.error(
                    f"Server did not become ready within {timeout} seconds (see --ready_timeout). "
                    f"Thread stacks:\n{format_all_thread_stacks()}"
                )
                raise TimeoutError(f"ModuleContainer didn't notify .ready in {timeout} seconds")

    @property
    def ready(self) -> mp.synchronize.Event:
        """
        An event (multiprocessing.Event) that is set when the container is ready to process requests.

        Example
        =======
        >>> container.start()
        >>> container.ready.wait(timeout=10)
        >>> print("Container ready" if container.ready.is_set() else "Container didn't start in 10 seconds")
        """
        return self.runtime.ready  # mp.Event that is true if self is ready to process batches

    def is_healthy(self) -> bool:
        return (
            self.dht_announcer.is_alive()
            and all(handler.is_alive() for handler in self.conn_handlers)
            and all(pool.is_alive() for pool in self.runtime.pools)
        )

    def shutdown(self):
        """
        Gracefully terminate the container, process-safe.
        Please note that terminating container otherwise (e.g. by killing processes) may result in zombie processes.
        If you did already cause a zombie outbreak, your only option is to kill them with -9 (SIGKILL).
        """
        self.dht_announcer.announce(ServerState.OFFLINE)
        logger.info(f"Announced that blocks {list(self.module_backends.keys())} are offline")

        self.ready.clear()

        logger.debug("Shutting down connection handlers")
        for handler in self.conn_handlers:
            handler.shutdown()

        logger.debug(f"Shutting down pools")
        for pool in self.runtime.pools:
            if pool.is_alive():
                pool.shutdown()

        logger.debug(f"Shutting down runtime")
        self.runtime.shutdown()

        logger.debug("Shutting down backends")
        for backend in self.module_backends.values():
            backend.shutdown()

        logger.info("Module container shut down successfully")


class ModuleAnnouncerThread(threading.Thread):
    """Periodically announces that this container hosts the specified modules, visible to all DHT peers"""

    def __init__(
        self,
        module_uids: List[str],
        dht: DHT,
        server_info: ServerInfo,
        model_info: ModelInfo,
        *,
        block_config: PretrainedConfig,
        memory_cache: MemoryCache,
        update_period: float,
        expiration: float,
        protocol_identity: Optional[NodeIdentity] = None,
        manifest_execution_profile: Optional[Dict[str, object]] = None,
        revocations: Optional[RevocationStore] = None,
        replay_guard: Optional[ReplayGuard] = None,
        max_pinged: int = 5,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.module_uids = module_uids
        self.dht = dht
        self.server_info = server_info
        self.model_info = model_info
        self.memory_cache = memory_cache

        self.bytes_per_token = block_config.hidden_size * get_size_in_bytes(DTYPE_MAP[server_info.torch_dtype])
        self.bytes_per_token //= block_config.num_key_value_groups

        self.update_period = update_period
        self.expiration = expiration
        self.protocol_identity = protocol_identity
        self.manifest_execution_profile = manifest_execution_profile
        self.revocations = revocations
        self.replay_guard = replay_guard
        self._announcement_sequence = time.time_ns()
        self.trigger = threading.Event()

        self.dht_prefix = parse_uid(module_uids[0])[0]
        block_indices = [parse_uid(uid)[1] for uid in module_uids]
        self.server_info.start_block = min(block_indices)
        self.server_info.end_block = max(block_indices) + 1

        self.max_pinged = max_pinged
        self.next_uids = [
            f"{self.dht_prefix}{UID_DELIMITER}{i}"
            for i in range(self.server_info.start_block + 1, self.server_info.end_block + 1)
        ]
        self.ping_aggregator = PingAggregator(self.dht)

    def run(self) -> None:
        while True:
            start_time = time.perf_counter()

            self.server_info.cache_tokens_left = self.memory_cache.bytes_left // self.bytes_per_token
            if self.server_info.state != ServerState.OFFLINE:
                self._ping_next_servers()
                self.server_info.next_pings = {
                    peer_id.to_base58(): rtt
                    for peer_id, rtt in self.ping_aggregator.to_dict().items()
                    if math.isfinite(rtt)
                }
            else:
                self.server_info.next_pings = None  # No need to ping if we're disconnecting

            expiration_time = get_dht_time() + self.expiration
            if self.server_info.manifest_digest is not None:
                if self.protocol_identity is None or self.manifest_execution_profile is None:
                    raise ProtocolSecurityError("Manifested workers cannot publish unsigned announcements")
                issued_at = get_dht_time()
                self._announcement_sequence = max(self._announcement_sequence + 1, time.time_ns())
                self.server_info.signed_announcement = None
                announcement = create_worker_announcement(
                    self.protocol_identity,
                    dht_prefix=self.dht_prefix,
                    manifest_digest=self.server_info.manifest_digest,
                    execution_profile=self.manifest_execution_profile,
                    server_info=self.server_info.signed_payload(),
                    issued_at=issued_at,
                    expires_at=expiration_time,
                    sequence=self._announcement_sequence,
                )
                self.server_info.signed_announcement = announcement.to_dict()

            declare_active_modules(
                self.dht,
                self.module_uids,
                self.server_info,
                expiration_time=expiration_time,
            )
            if self.server_info.state == ServerState.OFFLINE:
                break
            if not self.dht_prefix.startswith("_"):  # Not private
                self.dht.store(
                    key="_drift.models",
                    subkey=self.dht_prefix,
                    value=self.model_info.to_dict(),
                    expiration_time=get_dht_time() + self.expiration,
                )

            delay = self.update_period - (time.perf_counter() - start_time)
            if delay < 0:
                logger.warning(
                    f"Declaring blocks to DHT takes more than --update_period, consider increasing it (currently {self.update_period})"
                )
            self.trigger.wait(max(delay, 0))
            self.trigger.clear()

    def announce(self, state: ServerState) -> None:
        self.server_info.state = state
        self.trigger.set()
        if state == ServerState.OFFLINE:
            self.join()

    def _ping_next_servers(self) -> Dict[PeerID, float]:
        module_infos = get_remote_module_infos(
            self.dht,
            self.next_uids,
            manifest_digest=self.server_info.manifest_digest,
            manifest_execution_profile=self.manifest_execution_profile,
            revocations=self.revocations,
            replay_guard=self.replay_guard,
            latest=True,
        )
        middle_servers = {peer_id for info in module_infos[:-1] for peer_id in info.servers}
        pinged_servers = set(sample_up_to(middle_servers, self.max_pinged))
        pinged_servers.discard(self.dht.peer_id)
        # Sample servers hosting the block after the last one (most likely continuations) separately
        pinged_servers |= set(sample_up_to(module_infos[-1].servers, self.max_pinged))
        self.ping_aggregator.ping(list(pinged_servers))


class RuntimeWithDeduplicatedPools(Runtime):
    """A version of hivemind.moe.server.runtime.Runtime that allows multiple backends to reuse a task pool"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pools = tuple(set(self.pools))

    def iterate_minibatches_from_pools(self, timeout=None):
        # multiprocessing.connection.wait() delegates to WaitForMultipleObjects on Windows, which
        # accepts at most 63 handles. A backend contributes forward/backward pools and manifested
        # full-model workers can exceed that limit (Gemma 4 has 35 blocks / 72 handles). Polling each
        # pipe separately preserves one runtime and its shared inference pool without a busy spin.
        max_windows_wait_handles = 63
        if os.name != "nt" or len(self.pools) + 1 <= max_windows_wait_handles:
            yield from super().iterate_minibatches_from_pools(timeout)
            return

        while True:
            if self.shutdown_recv.poll():
                return
            ready_pools = [pool for pool in self.pools if pool.batch_receiver.poll()]
            if not ready_pools:
                time.sleep(0.001)
                continue

            pool = min(ready_pools, key=lambda candidate: candidate.priority)
            batch_index, batch_tensors = pool.load_batch_to_runtime(timeout, self.device)
            yield pool, batch_index, batch_tensors

    def run(self):
        # The runtime executes every backend's inference/forward (hence MemoryCache.use_cache) on this
        # thread. Claim it as the runtime thread before serving so the cache can distinguish the runtime
        # from connection handlers even before the first use_cache (esp. on Windows thread-mode).
        for backend in self.module_backends.values():
            backend.memory_cache.mark_runtime_thread()
        super().run()
