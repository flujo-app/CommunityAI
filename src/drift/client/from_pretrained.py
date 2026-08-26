import contextlib
import json
import os
import re
import tempfile
from contextvars import ContextVar
from typing import List, Optional, Tuple, Union

from hivemind.utils.logging import get_logger
from transformers import BloomPreTrainedModel, configuration_utils, modeling_utils

from drift.model_manifest import ManifestArtifactVerifier
from drift.utils.version import get_compatible_model_repo

logger = get_logger(__name__)


class FromPretrainedMixin:
    @classmethod
    def from_pretrained(
        cls,
        model_name_or_path: Union[str, os.PathLike, None],
        *args,
        low_cpu_mem_usage: Optional[bool] = None,
        **kwargs,
    ):
        model_name_or_path = get_compatible_model_repo(model_name_or_path)
        artifact_verifier = kwargs.pop("artifact_verifier", None)
        if low_cpu_mem_usage is None:
            low_cpu_mem_usage = True

        verifier_token = None
        if artifact_verifier is not None:
            if not isinstance(artifact_verifier, ManifestArtifactVerifier):
                raise TypeError("artifact_verifier must be a ManifestArtifactVerifier")
            artifact_verifier.ensure_startup_metadata()
            if not artifact_verifier.manifest.artifacts_for_roles({"weight_index"}):
                for artifact in artifact_verifier.manifest.artifacts_for_roles({"weight"}):
                    artifact_verifier.ensure_path(artifact.path, allowed_roles={"weight"})
            model_name_or_path = artifact_verifier.snapshot_root
            kwargs["local_files_only"] = True
            kwargs.pop("revision", None)
            kwargs.pop("force_download", None)
            verifier_token = _artifact_verifier.set(artifact_verifier)
        try:
            with ignore_keys(cls._keys_to_ignore_on_load_unexpected):
                return super().from_pretrained(
                    model_name_or_path,
                    *args,
                    low_cpu_mem_usage=low_cpu_mem_usage,
                    **kwargs,
                )
        finally:
            if verifier_token is not None:
                _artifact_verifier.reset(verifier_token)

    from_pretrained.__doc__ = BloomPreTrainedModel.from_pretrained.__doc__.replace(
        "low_cpu_mem_usage(`bool`, *optional*)",
        "low_cpu_mem_usage(`bool`, *optional*, defaults to `True` in DRIFT-LLM)",
    ).replace(
        "torch_dtype (`str` or `torch.dtype`, *optional*)",
        'torch_dtype (`str` or `torch.dtype`, *optional*, defaults to `"auto"` in DRIFT-LLM)',
    )


_ignored_keys = ContextVar("ignored_keys", default=None)
_artifact_verifier: ContextVar[Optional[ManifestArtifactVerifier]] = ContextVar("artifact_verifier", default=None)


@contextlib.contextmanager
def ignore_keys(patterns: List[str]):
    token = _ignored_keys.set(patterns)
    try:
        yield
    finally:
        _ignored_keys.reset(token)


def patched_get_checkpoint_shard_files(
    pretrained_model_name_or_path, index_filename, *args, **kwargs
) -> Tuple[List[str], dict]:
    """Same as modeling_utils.get_checkpoint_shard_files(), but does not download shards for the ignored keys."""

    should_ignore_keys = _ignored_keys.get() is not None
    verifier = _artifact_verifier.get()
    tempdir_ctx = tempfile.TemporaryDirectory() if should_ignore_keys else contextlib.nullcontext()
    with tempdir_ctx as tempdir:
        index = None
        if should_ignore_keys or verifier is not None:
            with open(index_filename) as f:
                index = json.load(f)
        if should_ignore_keys:
            n_original_shards = len(set(index["weight_map"].values()))

            index["weight_map"] = {
                param_name: filename
                for param_name, filename in index["weight_map"].items()
                if all(re.search(pattern, param_name) is None for pattern in _ignored_keys.get())
            }
            n_loaded_shards = len(set(index["weight_map"].values()))
            logger.debug(f"Loading {n_loaded_shards} shards out of {n_original_shards}")

            # Replace the original index with a patched JSON, where ignored keys are removed
            index_filename = os.path.join(tempdir, "pytorch_model.bin.index.json")
            with open(index_filename, "w") as f:
                json.dump(index, f)

        if verifier is not None:
            for filename in set(index["weight_map"].values()):
                verifier.ensure_path(filename, allowed_roles={"weight"})
            pretrained_model_name_or_path = verifier.snapshot_root
            kwargs["local_files_only"] = True
            kwargs.pop("revision", None)

        return original_get_checkpoint_shard_files(pretrained_model_name_or_path, index_filename, *args, **kwargs)


original_get_checkpoint_shard_files = modeling_utils.get_checkpoint_shard_files
modeling_utils.get_checkpoint_shard_files = patched_get_checkpoint_shard_files


def patched_get_resolved_checkpoint_files(*args, **kwargs):
    checkpoint_files, sharded_metadata = original_get_resolved_checkpoint_files(*args, **kwargs)
    verifier = _artifact_verifier.get()
    if verifier is not None:
        verifier.verify_checkpoint_files(checkpoint_files or ())
    return checkpoint_files, sharded_metadata


def patched_config_cached_file(*args, **kwargs):
    resolved = original_config_cached_file(*args, **kwargs)
    verifier = _artifact_verifier.get()
    if verifier is not None and resolved is not None:
        verifier.verify_resolved_file(resolved, allowed_roles={"config"})
    return resolved


original_get_resolved_checkpoint_files = modeling_utils._get_resolved_checkpoint_files
modeling_utils._get_resolved_checkpoint_files = patched_get_resolved_checkpoint_files
original_config_cached_file = configuration_utils.cached_file
configuration_utils.cached_file = patched_config_cached_file
