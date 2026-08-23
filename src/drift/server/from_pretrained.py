"""
Utils for fetching pretrained model parts. Currently, this relies on huggingface transformers' from_pretrained code.
If necessary, one can rewrite this to implement a different behavior, such as:
 - loading files from a local data source (e.g. S3)
 - load files via BitTorrent ( https://pypi.org/project/libtorrent/ ) or IPFS( https://docs.ipfs.io/how-to )
 - fetch the weights over IPoAC, using a fleet of trained pigeons ( http://www.faqs.org/rfcs/rfc1149.html )

"""
import json
import re
import time
from contextlib import suppress
from typing import Dict, Optional, Union

import safetensors
import torch
import torch.nn as nn
from accelerate import init_empty_weights
from accelerate.utils import set_module_tensor_to_device
from hivemind.utils.logging import get_logger
from huggingface_hub import get_hf_file_metadata, hf_hub_url
from huggingface_hub.utils import EntryNotFoundError
from transformers import PretrainedConfig, PreTrainedModel

from drift.constants import DTYPE_MAP
from drift.model_manifest import ManifestArtifactVerifier, ManifestError
from drift.server.block_utils import get_model_block, resolve_block_dtype
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.disk_cache import (
    DEFAULT_CACHE_DIR,
    allow_cache_reads,
    allow_cache_writes,
    free_disk_space_for,
    get_file_from_repo,
)
from drift.utils.weight_conversion import maybe_convert_block_state_dict

logger = get_logger(__name__)

# Older Transformers checkpoints may persist rotary frequencies below the attention module.
# Transformers 5 derives these values from the pinned config at the model level instead, and the
# DRIFT wrappers mirror that layout as ``block.rotary_emb``.  Treat only these exact legacy buffer
# names as derived compatibility state; every other unconsumed key remains a correctness warning.
_LEGACY_DERIVED_ROTARY_BUFFER = re.compile(
    r"^(?:self_attn|self_attention)\.rotary_emb\.(?:inv_freq|original_inv_freq)$"
)


def _find_unconsumed_checkpoint_keys(block: nn.Module, state_dict: "StateDict") -> list[str]:
    ignored = [re.compile(pattern) for pattern in getattr(block, "_keys_to_ignore_on_load_unexpected", None) or []]
    module_tensor_names = {name for name, _ in block.named_parameters()} | {name for name, _ in block.named_buffers()}
    return sorted(
        key
        for key in state_dict
        if key not in module_tensor_names
        and not _LEGACY_DERIVED_ROTARY_BUFFER.fullmatch(key)
        and not any(pattern.search(key) for pattern in ignored)
    )


def load_pretrained_block(
    model_name: str,
    block_index: int,
    *,
    config: Optional[PretrainedConfig] = None,
    torch_dtype: Union[torch.dtype, str] = "auto",
    revision: Optional[str] = None,
    token: Optional[Union[str, bool]] = None,
    cache_dir: Optional[str] = None,
    max_disk_space: Optional[int] = None,
    artifact_verifier: Optional[ManifestArtifactVerifier] = None,
) -> nn.Module:
    if config is None:
        config_source = artifact_verifier.ensure_startup_metadata() if artifact_verifier is not None else model_name
        config = AutoDistributedConfig.from_pretrained(
            config_source,
            token=token,
            revision=None if artifact_verifier is not None else revision,
            local_files_only=artifact_verifier is not None,
        )
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR

    assert torch_dtype in DTYPE_MAP.values(), f"torch_dtype must be one of {list(DTYPE_MAP.values())}"
    torch_dtype = resolve_block_dtype(config, torch_dtype)

    with init_empty_weights():
        block = get_model_block(config, layer_idx=block_index)

    block_prefix = f"{config.block_prefix}.{block_index}."
    state_dict = _load_state_dict_from_repo(
        model_name,
        block_prefix,
        revision=revision,
        token=token,
        cache_dir=cache_dir,
        max_disk_space=max_disk_space,
        artifact_verifier=artifact_verifier,
    )

    # transformers >=5.0 may restructure weights when loading (e.g. Mixtral fuses per-expert
    # weights). DRIFT-LLM loads block weights by name, so apply the same conversion here.
    state_dict = maybe_convert_block_state_dict(config, state_dict, block)

    for param_name, _ in block.named_parameters():
        assert param_name in state_dict, f"{param_name} not in state dict"
        param = state_dict[param_name]
        if not str(param.dtype).startswith(("torch.uint", "torch.int", "torch.bool")):
            param = param.to(torch_dtype)
        set_module_tensor_to_device(block, param_name, "cpu", value=param, dtype=param.dtype)

    # Persistent buffers live in the checkpoint too (e.g. Gemma 4's trained per-layer
    # `layer_scalar`, clipped-linear ranges); skipping them silently leaves init values.
    # Buffers absent from the checkpoint (rotary inv_freq, embed scales) keep their
    # computed values, so unlike parameters they are not required to be present.
    for buffer_name, _ in block.named_buffers():
        if buffer_name not in state_dict:
            continue
        buffer = state_dict[buffer_name]
        if not str(buffer.dtype).startswith(("torch.uint", "torch.int", "torch.bool")):
            buffer = buffer.to(torch_dtype)
        set_module_tensor_to_device(block, buffer_name, "cpu", value=buffer, dtype=buffer.dtype)

    # Any checkpoint key under this block's prefix that matched neither a parameter nor a buffer
    # is trained state being silently dropped (how Gemma 4's layer_scalar went missing) -- unless
    # the block declares it ignored by design (e.g. the k/v projections checkpoints ship for
    # KV-shared consumer layers, which the module intentionally does not have).
    leftover = _find_unconsumed_checkpoint_keys(block, state_dict)
    if leftover:
        logger.warning(
            f"Block {block_index}: checkpoint keys were not loaded into {type(block).__name__}: {leftover}. "
            f"The block will run without this trained state and may produce wrong outputs."
        )

    logger.info(f"Loaded {model_name} block {block_index}")
    return block


StateDict = Dict[str, torch.Tensor]


def _load_state_dict_from_repo(
    model_name: str,
    block_prefix: str,
    *,
    revision: Optional[str] = None,
    token: Optional[Union[str, bool]] = None,
    cache_dir: str,
    max_disk_space: Optional[int] = None,
    artifact_verifier: Optional[ManifestArtifactVerifier] = None,
) -> StateDict:
    index_file = _find_index_file(
        model_name,
        revision=revision,
        token=token,
        cache_dir=cache_dir,
        artifact_verifier=artifact_verifier,
    )
    if index_file.endswith(".index.json"):  # Sharded model
        path = (
            str(artifact_verifier.ensure_path(index_file, allowed_roles={"weight_index"}))
            if artifact_verifier is not None
            else get_file_from_repo(
                model_name,
                filename=index_file,
                revision=revision,
                use_auth_token=token,
                cache_dir=cache_dir,
            )
        )
        if path is None:
            # _find_index_file() told that a file exists but we can't get it (e.g., it just disappeared)
            raise ValueError(f"Failed to get file {index_file}")

        with open(path) as f:
            index = json.load(f)
        filenames = {
            filename for param_name, filename in index["weight_map"].items() if param_name.startswith(block_prefix)
        }
        if not filenames:
            raise RuntimeError(f"Block {block_prefix}* not found in the index: {index['weight_map']}")
    else:  # Non-sharded model
        filenames = {index_file}
    logger.debug(f"Loading {block_prefix}* from {filenames}")

    state_dict = {}
    for filename in filenames:
        shard_state_dict = _load_state_dict_from_repo_file(
            model_name,
            filename,
            block_prefix=block_prefix,
            revision=revision,
            token=token,
            cache_dir=cache_dir,
            max_disk_space=max_disk_space,
            artifact_verifier=artifact_verifier,
        )
        shard_state_dict = {
            param_name[len(block_prefix) :]: param
            for param_name, param in shard_state_dict.items()
            if param_name.startswith(block_prefix)
        }  # Remove unused parameters from memory
        state_dict.update(shard_state_dict)
    return state_dict


INDEX_FILES = ["model.safetensors.index.json", "model.safetensors", "pytorch_model.bin.index.json", "pytorch_model.bin"]


def _find_index_file(
    model_name: str,
    *,
    revision: Optional[str] = None,
    token: Optional[Union[str, bool]] = None,
    cache_dir: str,
    artifact_verifier: Optional[ManifestArtifactVerifier] = None,
) -> str:
    if artifact_verifier is not None:
        indices = artifact_verifier.manifest.artifacts_for_roles({"weight_index"})
        if len(indices) > 1:
            raise ManifestError("Manifest declares more than one checkpoint index")
        if indices:
            artifact_verifier.ensure_path(indices[0].path, allowed_roles={"weight_index"})
            return indices[0].path
        checkpoints = {
            artifact.path: artifact
            for artifact in artifact_verifier.manifest.artifacts_for_roles(
                {"weight", "converted_weight", "quantized_weight"}
            )
        }
        for filename in INDEX_FILES:
            if filename in checkpoints:
                artifact_verifier.ensure_path(
                    filename, allowed_roles={"weight", "converted_weight", "quantized_weight"}
                )
                return filename
        raise ManifestError("Manifest does not declare a checkpoint format supported by the block loader")

    # If we have cached weights (e.g., Pickle from older DRIFT-LLM versions), reuse them
    for filename in INDEX_FILES:
        path = get_file_from_repo(
            model_name,
            filename,
            revision=revision,
            use_auth_token=token,
            cache_dir=cache_dir,
            local_files_only=True,
        )
        if path is not None:
            return filename

    # If we don't, prefer Safetensors when possible
    # (we don't download files here since we can't account for max_disk_space in case of large files)
    for filename in INDEX_FILES:
        with suppress(EntryNotFoundError):
            get_hf_file_metadata(hf_hub_url(model_name, filename, revision=revision), token=token)
            return filename

    raise ValueError(
        f"Repo {model_name} does not contain weights in a supported format: files {INDEX_FILES} do not exist"
    )


def _load_state_dict_from_repo_file(
    model_name: str,
    filename: str,
    *,
    block_prefix: Optional[str] = None,
    revision: Optional[str] = None,
    token: Optional[Union[str, bool]] = None,
    cache_dir: str,
    max_disk_space: Optional[int] = None,
    delay: float = 30,
    artifact_verifier: Optional[ManifestArtifactVerifier] = None,
) -> StateDict:
    if artifact_verifier is not None:
        path = artifact_verifier.ensure_path(filename, allowed_roles={"weight", "converted_weight", "quantized_weight"})
        with allow_cache_reads(cache_dir):
            # Re-check after acquiring the shared cache lock, then keep the artifact alive through deserialization.
            path = artifact_verifier.ensure_path(
                filename, allowed_roles={"weight", "converted_weight", "quantized_weight"}
            )
            return _load_state_dict_from_local_file(str(path), block_prefix=block_prefix)

    # First, try to find the weights locally
    try:
        with allow_cache_reads(cache_dir):
            path = get_file_from_repo(
                model_name,
                filename,
                revision=revision,
                use_auth_token=token,
                cache_dir=cache_dir,
                local_files_only=True,
            )
            if path is not None:
                return _load_state_dict_from_local_file(path, block_prefix=block_prefix)
    except Exception:
        logger.warning(f"Cache for file {filename} is corrupted, it will be downloaded again", exc_info=True)

    # If not found, ensure that we have enough disk space to download them (maybe remove something)
    while True:
        try:
            with allow_cache_writes(cache_dir):
                url = hf_hub_url(model_name, filename, revision=revision)
                file_size = get_hf_file_metadata(url, token=token).size
                if file_size is not None:
                    free_disk_space_for(file_size, cache_dir=cache_dir, max_disk_space=max_disk_space)
                else:
                    logger.warning(f"Failed to fetch size of file {filename} from repo {model_name}")

                path = get_file_from_repo(
                    model_name,
                    filename,
                    revision=revision,
                    use_auth_token=token,
                    cache_dir=cache_dir,
                    local_files_only=False,
                )
                if path is None:
                    raise RuntimeError(f"File {filename} does not exist in repo {model_name}")
                return _load_state_dict_from_local_file(path, block_prefix=block_prefix)
        except Exception as e:
            logger.warning(f"Failed to load file {filename} from HF Hub (retry in {delay:.0f} sec)", exc_info=True)
            time.sleep(delay)


def _load_state_dict_from_local_file(path: str, *, block_prefix: Optional[str] = None) -> StateDict:
    if path.endswith(".bin"):
        return torch.load(path, map_location="cpu")

    if path.endswith(".safetensors"):
        with safetensors.safe_open(path, framework="pt", device="cpu") as f:
            # ``get_tensor`` returns a view backed by the file mapping. A manifested worker loads one
            # block at a time, so retaining those views can retain one mapping of the complete shard
            # per block. Large same-dtype checkpoints exhausted Windows commit/virtual memory long
            # before their actual selected block tensors filled RAM. Copy only the selected tensors
            # while the mapping is open; the caller then owns ordinary CPU storage and the complete
            # shard mapping can close before the next block is loaded.
            return {
                key: f.get_tensor(key).clone()
                for key in f.keys()
                if block_prefix is None or key.startswith(block_prefix)
            }

    raise ValueError(f"Unknown weight format: {path}")
