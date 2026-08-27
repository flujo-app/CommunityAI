"""Offline loading tests for the Qwen3.5 multimodal wrapper's text tower."""

import os

import pytest
import torch
from hivemind import DHT
from hivemind.proto.runtime_pb2 import CompressionType
from safetensors.torch import save_file

from drift.client.remote_sequential import RemoteSequential
from drift.data_structures import ModelInfo, ServerInfo, ServerState
from drift.models.qwen3_5.config import DistributedQwen3_5Config, is_multimodal_wrapper_checkpoint
from drift.models.qwen3_5.model import _Qwen3_5WrapperLoadMixin
from drift.server.from_pretrained import load_pretrained_block
from drift.server.server import ModuleContainer
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.convert_block import QuantType
from drift.utils.reference_model import load_reference_model_for_causal_lm

ATOL = 3e-5
_WRAPPER_KEY_MAPPING = {r"^model\.language_model\.": "model."}


def _tiny_text_config():
    return DistributedQwen3_5Config(
        vocab_size=128,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=4,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=16,
        linear_num_key_heads=2,
        linear_num_value_heads=2,
        linear_key_head_dim=8,
        linear_value_head_dim=8,
        linear_conv_kernel_dim=4,
        max_position_embeddings=128,
        tie_word_embeddings=True,
    )


@pytest.fixture(scope="module")
def wrapper_checkpoint(tmp_path_factory):
    from transformers.models.qwen3_5 import Qwen3_5Config, Qwen3_5ForCausalLM, Qwen3_5VisionConfig

    text_config = _tiny_text_config()
    torch.manual_seed(0)
    reference = Qwen3_5ForCausalLM(text_config).eval()
    state_dict = {}
    for key, value in reference.state_dict().items():
        mapped = f"model.language_model.{key.removeprefix('model.')}" if key.startswith("model.") else key
        state_dict[mapped] = value.detach().clone()
    state_dict["model.visual.patch_embed.proj.weight"] = torch.randn(8, 3, 2, 2, 2)

    path = tmp_path_factory.mktemp("qwen3_5_wrapper")
    save_file(state_dict, os.path.join(path, "model.safetensors"), metadata={"format": "pt"})
    outer_config = Qwen3_5Config(
        text_config=text_config.to_dict(),
        vision_config=Qwen3_5VisionConfig(
            depth=1,
            hidden_size=32,
            intermediate_size=64,
            num_heads=4,
            out_hidden_size=64,
            num_position_embeddings=16,
        ).to_dict(),
        tie_word_embeddings=True,
    )
    outer_config.architectures = ["Qwen3_5ForConditionalGeneration"]
    outer_config.save_pretrained(path)
    return str(path), reference


@pytest.fixture(scope="module")
def text_only_checkpoint(tmp_path_factory):
    from transformers.models.qwen3_5 import Qwen3_5ForCausalLM

    config = _tiny_text_config()
    torch.manual_seed(0)
    model = Qwen3_5ForCausalLM(config).eval()
    path = tmp_path_factory.mktemp("qwen3_5_text")
    save_file(
        {key: value.detach().clone() for key, value in model.state_dict().items()},
        os.path.join(path, "model.safetensors"),
        metadata={"format": "pt"},
    )
    config.save_pretrained(path)
    return str(path)


def test_qwen3_5_wrapper_detection_and_dispatch(wrapper_checkpoint, text_only_checkpoint):
    path, _ = wrapper_checkpoint
    assert is_multimodal_wrapper_checkpoint(path)
    assert not is_multimodal_wrapper_checkpoint(text_only_checkpoint)

    config = AutoDistributedConfig.from_pretrained(path)
    assert type(config).__name__ == "DistributedQwen3_5Config"
    assert config.model_type == "qwen3_5_text"
    assert config._source_architectures == ("Qwen3_5ForConditionalGeneration",)
    assert config.block_prefix == "model.language_model.layers"
    assert AutoDistributedConfig.from_pretrained(text_only_checkpoint).block_prefix == "model.layers"


def test_qwen3_5_cache_budget_includes_fixed_recurrent_state():
    config = _tiny_text_config()
    strategy = config.kv_cache_strategy
    dtype = torch.float32
    max_length = 1

    standard_bytes = (
        2 * config.hidden_size * max_length // config.num_key_value_groups * torch.empty((), dtype=dtype).element_size()
    )
    conv_dim = (
        2 * config.linear_num_key_heads * config.linear_key_head_dim
        + config.linear_num_value_heads * config.linear_value_head_dim
    )
    recurrent_state_bytes = (
        conv_dim * config.linear_conv_kernel_dim * torch.empty((), dtype=dtype).element_size()
        + config.linear_num_value_heads
        * config.linear_key_head_dim
        * config.linear_value_head_dim
        * torch.empty((), dtype=torch.float32).element_size()
    )
    linear_index = config.layer_types.index("linear_attention")
    full_index = config.layer_types.index("full_attention")

    assert recurrent_state_bytes > standard_bytes
    assert (
        strategy.estimate_cache_bytes(config, max_length, dtype=dtype, block_index=linear_index)
        == recurrent_state_bytes
    )
    assert strategy.estimate_cache_bytes(config, max_length, dtype=dtype, block_index=full_index) == standard_bytes
    assert strategy.estimate_cache_bytes(config, max_length, dtype=dtype) == recurrent_state_bytes


def test_qwen3_5_wrapper_blocks_load_and_match(wrapper_checkpoint):
    path, reference = wrapper_checkpoint
    config = AutoDistributedConfig.from_pretrained(path)
    input_ids = torch.randint(0, config.vocab_size, (1, 6))
    layer_inputs, layer_outputs = {}, {}

    def pre_hook(index):
        def hook(_module, args, kwargs):
            layer_inputs[index] = (args[0] if args else kwargs["hidden_states"]).detach().clone()

        return hook

    def output_hook(index):
        def hook(_module, _args, _kwargs, output):
            layer_outputs[index] = (output[0] if isinstance(output, tuple) else output).detach().clone()

        return hook

    for index, layer in enumerate(reference.model.layers):
        layer.register_forward_pre_hook(pre_hook(index), with_kwargs=True)
        layer.register_forward_hook(output_hook(index), with_kwargs=True)
    with torch.inference_mode():
        reference(input_ids, use_cache=False)

    for index in range(config.num_hidden_layers):
        block = load_pretrained_block(path, index, config=config, torch_dtype=torch.float32).eval()
        with torch.inference_mode():
            (actual,) = block(layer_inputs[index])
        assert torch.allclose(actual, layer_outputs[index], atol=ATOL), (
            index,
            (actual - layer_outputs[index]).abs().max(),
        )


def test_qwen3_5_wrapper_mixin_injects_text_key_mapping(wrapper_checkpoint, text_only_checkpoint):
    path, _ = wrapper_checkpoint
    captured = {}

    class _Base:
        @classmethod
        def from_pretrained(cls, model_name_or_path, *args, **kwargs):
            captured["kwargs"] = kwargs
            return "loaded"

    class _Model(_Qwen3_5WrapperLoadMixin, _Base):
        pass

    assert _Model.from_pretrained(path) == "loaded"
    assert captured["kwargs"].get("key_mapping") == _WRAPPER_KEY_MAPPING
    captured.clear()
    _Model.from_pretrained(text_only_checkpoint)
    assert "key_mapping" not in captured["kwargs"]


def test_qwen3_5_wrapper_uses_verified_snapshot_for_offline_detection(text_only_checkpoint):
    captured = {}

    class _Verifier:
        snapshot_root = text_only_checkpoint

    class _Base:
        @classmethod
        def from_pretrained(cls, model_name_or_path, *args, **kwargs):
            captured["model_name_or_path"] = model_name_or_path
            captured["kwargs"] = kwargs
            return "loaded"

    class _Model(_Qwen3_5WrapperLoadMixin, _Base):
        pass

    verifier = _Verifier()
    assert _Model.from_pretrained("Qwen/Qwen3.5-2B", artifact_verifier=verifier, local_files_only=True) == "loaded"
    assert captured["model_name_or_path"] == "Qwen/Qwen3.5-2B"
    assert captured["kwargs"]["artifact_verifier"] is verifier
    assert "key_mapping" not in captured["kwargs"]


def test_qwen3_5_wrapper_key_mapping_loads_local_text_weights(wrapper_checkpoint):
    from transformers.models.qwen3_5 import Qwen3_5ForCausalLM

    path, reference = wrapper_checkpoint
    loaded = load_reference_model_for_causal_lm(
        path,
        torch_dtype=torch.float32,
    )
    assert isinstance(loaded, Qwen3_5ForCausalLM)
    assert torch.equal(loaded.model.embed_tokens.weight, reference.model.embed_tokens.weight)
    assert torch.equal(loaded.model.norm.weight, reference.model.norm.weight)
    assert torch.equal(loaded.lm_head.weight, reference.lm_head.weight)


def test_qwen3_5_remote_prefill_and_decode_matches_stock(wrapper_checkpoint):
    """Serve all hybrid blocks through a real local Hivemind RPC route."""

    path, reference = wrapper_checkpoint
    config = AutoDistributedConfig.from_pretrained(path)
    config._attn_implementation = "eager"
    config.dht_prefix = "qwen3-5-offline-parity"
    server_dht = DHT(
        start=True,
        num_workers=config.num_hidden_layers,
        use_relay=False,
        use_auto_relay=False,
        client_mode=False,
        host_maddrs=["/ip4/127.0.0.1/tcp/0"],
    )
    peers = [str(address) for address in server_dht.get_visible_maddrs()]
    container = None
    client_dht = None
    remote = None
    try:
        cache_tokens = 128
        cache_bytes = sum(
            config.kv_cache_strategy.estimate_cache_bytes(
                config,
                cache_tokens,
                dtype=torch.float32,
                block_index=block_index,
            )
            for block_index in range(config.num_hidden_layers)
        )
        container = ModuleContainer.create(
            dht=server_dht,
            dht_prefix=config.dht_prefix,
            converted_model_name_or_path=path,
            block_config=config,
            attn_cache_bytes=cache_bytes,
            server_info=ServerInfo(
                state=ServerState.JOINING,
                throughput=1.0,
                torch_dtype="float32",
                quant_type=QuantType.NONE.name.lower(),
                using_relay=False,
            ),
            model_info=ModelInfo(num_blocks=config.num_hidden_layers, repository=path),
            block_indices=list(range(config.num_hidden_layers)),
            num_handlers=1,
            min_batch_size=1,
            max_batch_size=8,
            max_chunk_size_bytes=4 * 1024 * 1024,
            max_alloc_timeout=10,
            paged_cache=False,
            inference_max_length=32,
            torch_dtype=torch.float32,
            cache_dir=path,
            max_disk_space=None,
            device=torch.device("cpu"),
            compression=CompressionType.NONE,
            stats_report_interval=None,
            update_period=1,
            expiration=30,
            request_timeout=20,
            session_timeout=20,
            step_timeout=20,
            prefetch_batches=1,
            sender_threads=1,
            revision=None,
            token=None,
            model_manifest=None,
            protocol_identity=None,
            manifest_execution_profile=None,
            quant_type=QuantType.NONE,
            tensor_parallel_devices=(torch.device("cpu"),),
            ready_timeout=30,
            start=True,
        )

        config.initial_peers = peers
        config.update_period = 0.2
        config.request_timeout = 20
        client_dht = DHT(initial_peers=peers, client_mode=True, start=True)
        remote = RemoteSequential(config, dht=client_dht)
        # make_sequence starts the manager's refresh thread; calling update(wait=True) before the
        # thread starts would wait forever without performing a DHT fetch.
        route = remote.sequence_manager.make_sequence(mode="min_latency", cache_tokens_needed=7)
        assert route and route[0].start == 0 and route[-1].end == config.num_hidden_layers

        input_ids = torch.randint(0, config.vocab_size, (1, 7))
        with torch.inference_mode():
            expected = reference.model(input_ids, use_cache=False).last_hidden_state

        outputs = []
        with torch.inference_mode(), remote.inference_session(max_length=input_ids.shape[1]):
            for start, end in ((0, 4), (4, 5), (5, 7)):
                hidden_states = reference.model.embed_tokens(input_ids[:, start:end])
                outputs.append(reference.model.norm(remote(hidden_states)))
        actual = torch.cat(outputs, dim=1)
        assert torch.allclose(actual, expected, atol=ATOL), (actual - expected).abs().max()
    finally:
        if remote is not None:
            remote.sequence_manager.shutdown()
        if client_dht is not None:
            client_dht.shutdown()
            client_dht.join()
        if container is not None:
            container.shutdown()
            container.join(timeout=10)
        server_dht.shutdown()
        server_dht.join()
