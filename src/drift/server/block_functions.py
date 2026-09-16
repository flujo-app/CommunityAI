"""
This module implements server-side computations on served blocks: forward, backward and inference; used by handler
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, Sequence, Tuple, Union

import torch
from hivemind.compression.serialization import deserialize_torch_tensor, serialize_torch_tensor
from hivemind.moe.expert_uid import ExpertUID
from hivemind.proto import runtime_pb2
from hivemind.utils.logging import get_logger
from hivemind.utils.nested import nested_flatten

from drift.data_structures import Handle, InferenceMetadata
from drift.server.admission import AdmissionRejected
from drift.server.backend import TransformerBackend
from drift.server.task_pool import PrioritizedTaskPool
from drift.server.task_prioritizer import TaskPrioritizerBase
from drift.utils.convert_block import QuantType
from drift.utils.misc import DUMMY, is_dummy
from drift.utils.packaging import unpack_args_kwargs
from drift.utils.shared_kv import flatten_shared_kv, unflatten_shared_kv

# We prioritize short inference requests and make them use a *merged* inference pool,
# so they are processed without interruptions and extra overheads
# TODO: Increase the NF4 threshold once bitsandbytes ships efficient NF4 kernel for parallel forward
MAX_SHORT_INFERENCE_TOKENS = 128
MAX_NF4_SHORT_INFERENCE_TOKENS = 1

logger = get_logger(__name__)


async def run_rpc_forward(
    *flat_tensors: torch.Tensor,
    requested_backends: Sequence[TransformerBackend],
    active_adapter: str = "",
    prioritizer: TaskPrioritizerBase,
    points: int = 0,
    args_structure: Any = None,
) -> torch.Tensor:
    """
    Run forward pass on deserialized inputs and prompts, used by rpc_forward and rpc_forward_stream

    :param flat_tensors: a list of tensors that includes first layer inputs, optional prompts and extra tensors
    :note: some input tensors can be missing, in which case they will be replaced with dummy tensors (see is_dummy)
    :param requested_backends: a sequence of transformer blocks in the same order as they appear in forward pass
    :returns: hidden states after the last layer [batch_size, seq_length, hid_size]
    """
    if args_structure is not None:
        # TODO: kwargs currently is unused, it can be used later for peft-like adaptation
        flat_tensors, kwargs = unpack_args_kwargs(flat_tensors, args_structure)
    hidden_states, prompts, *_ = flat_tensors

    dtype = requested_backends[0].dtype
    # check parse input tensors and cast dtypes
    hidden_states = hidden_states.to(dtype)
    assert hidden_states.ndim == 3
    if prompts is None or is_dummy(prompts):
        prompts = [DUMMY] * len(requested_backends)
    else:
        prompts = [p.squeeze(0) for p in prompts.to(requested_backends[0].dtype).split(1, dim=0)]

    # Run a chain of requested backends
    for backend, prompt in zip(requested_backends, prompts):
        if not is_dummy(prompt):
            hidden_states[:, : prompt.shape[1]] += prompt

        assert isinstance(backend.inference_pool, PrioritizedTaskPool), "drift support only prioritized pools"
        priority = prioritizer.prioritize(
            hidden_states, points=points / len(requested_backends), backend=backend, type="forward"
        )
        (hidden_states,) = await backend.forward_pool.submit_task(
            hidden_states,
            active_adapter,
            priority=priority,
        )
        assert isinstance(hidden_states, torch.Tensor)
        assert (
            hidden_states.ndim == 3
        ), f"inputs to {type(backend)} must be a list with a single 3d tensor of hidden states"

    return hidden_states


async def run_rpc_backward(
    *flat_tensors: torch.Tensor,
    requested_backends: Sequence[TransformerBackend],
    active_adapter: str = "",
    prioritizer: TaskPrioritizerBase,
    points: int = 0,
    args_structure: Any = None,
) -> Union[torch.Tensor, Sequence[torch.Tensor]]:
    if args_structure is not None:
        # TODO: kwargs currently is unused, it can be used later for peft-like adaptation
        flat_tensors, kwargs = unpack_args_kwargs(flat_tensors, args_structure)
    inputs, grad_outputs, prompts, *_ = flat_tensors

    # Cast inputs & grad outputs to backend dtype
    inputs = inputs.to(requested_backends[0].dtype)
    grad_outputs = grad_outputs.to(requested_backends[-1].dtype)

    if prompts is None or is_dummy(prompts):
        prompts = [DUMMY] * len(requested_backends)
    else:
        prompts = [p.squeeze(0) for p in prompts.to(requested_backends[0].dtype).split(1, dim=0)]

    # Run a forward chain to collect intermediate inputs
    # Note that we do not forward for the last module since we do not need its output
    inter_inputs = []
    for backend, prompt in zip(requested_backends[:-1], prompts[:-1]):
        assert inputs.ndim == 3, f"inputs to {type(backend)} must be a single 3d tensor of hidden states"
        if not is_dummy(prompt):
            inputs[:, : prompt.shape[1]] += prompt
        inter_inputs.append(inputs)
        assert isinstance(backend.inference_pool, PrioritizedTaskPool), "drift support only prioritized pools"
        priority = prioritizer.prioritize(
            inputs, points=points / len(requested_backends), backend=backend, type="forward_in_backward"
        )
        (inputs,) = await backend.forward_pool.submit_task(inputs, active_adapter, priority=priority)

        assert isinstance(inputs, torch.Tensor)

    if not is_dummy(prompts[-1]):
        inputs[:, : prompts[-1].shape[1]] += prompts[-1]
    inter_inputs.append(inputs)

    assert len(inter_inputs) == len(prompts) == len(requested_backends), "internal shape error during backward"
    grad_prompts_reversed = []
    # Run a chain of requested backends
    for inp, prompt, backend in zip(*map(reversed, (inter_inputs, prompts, requested_backends))):
        assert isinstance(backend.inference_pool, PrioritizedTaskPool), "drift support only prioritized pools"
        priority = prioritizer.prioritize(
            inp, grad_outputs, points=points / len(requested_backends), backend=backend, type="backward"
        )
        (grad_outputs,) = await backend.backward_pool.submit_task(inp, grad_outputs, active_adapter, priority=priority)

        assert isinstance(grad_outputs, torch.Tensor)
        if not is_dummy(prompt):
            grad_prompts_reversed.append(grad_outputs[:, : prompt.shape[1]].unsqueeze(0))

    grad_prompts = torch.cat(grad_prompts_reversed[::-1], dim=0) if grad_prompts_reversed else DUMMY
    return [grad_outputs] if is_dummy(grad_prompts) else [grad_outputs, grad_prompts]  # TODO un-duct-tape


async def iterate_rpc_inference(
    requested_uids: Sequence[ExpertUID],
    requested_backends: Sequence[TransformerBackend],
    active_adapter: Optional[str],
    input_iterator: AsyncIterator[Tuple[runtime_pb2.ExpertRequest, dict]],
    cache_handles: Sequence[Sequence[Handle]],
    *,
    max_length: int,
    session_batch_size: int,
    prioritizer: TaskPrioritizerBase,
    points: int,
    quant_type: QuantType,
    args_structure: Any = None,
) -> AsyncIterator[Tuple[Sequence[runtime_pb2.Tensor], bool, Dict, Dict]]:
    assert len(cache_handles) == len(requested_backends)

    prefix_length = 0
    point_per_piece = points / max_length if max_length > 0 else 0.0
    hidden_size = requested_backends[0].config.hidden_size
    max_step_tokens = min(backend.inference_pool.max_batch_size for backend in requested_backends)

    def validate_shape(shape):
        if len(shape) != 3 or any(isinstance(size, bool) or not isinstance(size, int) for size in shape):
            raise AdmissionRejected("inference activation shape is invalid")
        batch_size, length_increment, width = shape
        if (
            batch_size != session_batch_size
            or batch_size < 1
            or width != hidden_size
            or not 0 <= length_increment <= max_length - prefix_length
            or batch_size * max(length_increment, 1) > max_step_tokens
        ):
            raise AdmissionRejected("inference activation shape exceeds the session limits")
        return tuple(shape)

    async for request, step_metadata in input_iterator:
        if "start_from_position" in step_metadata:
            start_from_position = step_metadata["start_from_position"]
            # Validate wire metadata before decoding tensors or passing a cache offset to a worker.
            # Explicit checks also apply when Python runs with assertions disabled.
            if (
                isinstance(start_from_position, bool)
                or not isinstance(start_from_position, int)
                or not 0 <= start_from_position <= prefix_length
            ):
                raise AdmissionRejected("inference cache position is invalid")
            prefix_length = start_from_position

        # Tensor zero is the admission header. Packed arguments may select a different
        # activation, so validate that effective header too before decoding any tensors.
        if not request.tensors:
            raise AdmissionRejected("inference activation shape is invalid")
        validate_shape(request.tensors[0].size)
        wire_args = tuple(request.tensors)
        if args_structure is not None:
            try:
                wire_args, _ = unpack_args_kwargs(wire_args, args_structure)
            except (TypeError, ValueError, IndexError, KeyError) as exc:
                raise AdmissionRejected("inference activation arguments are invalid") from exc
        if (
            not isinstance(wire_args, (tuple, list))
            or len(wire_args) < 3
            or not isinstance(wire_args[0], runtime_pb2.Tensor)
        ):
            raise AdmissionRejected("inference activation arguments are invalid")
        activation_shape = validate_shape(wire_args[0].size)

        flat_tensors = tuple(deserialize_torch_tensor(tensor) for tensor in request.tensors)
        if args_structure is not None:
            # TODO: kwargs currently is unused, it can be used later for peft-like adaptation
            flat_tensors, kwargs = unpack_args_kwargs(flat_tensors, args_structure)

        hidden_states, prompts, hypo_ids, *rest = flat_tensors
        if not isinstance(hidden_states, torch.Tensor) or tuple(hidden_states.shape) != activation_shape:
            raise AdmissionRejected("inference decoded activation shape is invalid")
        batch_size, length_increment, _ = validate_shape(hidden_states.shape)
        per_layer_inputs = rest[0] if rest else None  # Gemma 4 Per-Layer Embedding slices (optional)
        shared_kv_input_tensors = list(rest[1:])  # Gemma 4 KV-sharing donor K/V from upstream spans (optional)

        # Reject malformed beam indices before any worker can reorder its cache, including
        # zero-token steps. The empty int64 vector is the client's no-reordering sentinel.
        if not isinstance(hypo_ids, torch.Tensor) or hypo_ids.dtype != torch.int64 or hypo_ids.ndim != 1:
            raise AdmissionRejected("inference hypothesis indices are invalid")
        if hypo_ids.numel() and (
            hypo_ids.numel() != batch_size or torch.any(hypo_ids < 0).item() or torch.any(hypo_ids >= batch_size).item()
        ):
            raise AdmissionRejected("inference hypothesis indices are invalid")

        # Cast inputs to backend dtype
        hidden_states = hidden_states.to(requested_backends[0].dtype)

        # parse deep prompts (optional argument)
        has_prompts = prompts is not None and not is_dummy(prompts)
        if not has_prompts:
            prompts = [None] * len(requested_backends)
        else:
            prompts = [p.squeeze(0) for p in prompts.to(requested_backends[0].dtype).split(1, dim=0)]
            prompts = [prompt if not is_dummy(prompt) else None for prompt in prompts]

        if not (len(requested_backends) == len(prompts)):
            raise ValueError(f"Received {len(prompts)} prompts for {len(requested_backends)} backends")

        # parse per-layer inputs (optional; one [batch, seq, per_layer_dim] tensor per backend)
        has_per_layer_inputs = per_layer_inputs is not None and not is_dummy(per_layer_inputs)
        if not has_per_layer_inputs:
            per_layer_inputs = [None] * len(requested_backends)
        else:
            per_layer_inputs = [p.squeeze(0) for p in per_layer_inputs.to(requested_backends[0].dtype).split(1, dim=0)]
        if len(requested_backends) != len(per_layer_inputs):
            raise ValueError(
                f"Received {len(per_layer_inputs)} per-layer inputs for {len(requested_backends)} backends"
            )

        # Donor keys/values for KV-sharing (Gemma 4) live in this per-span dict: donor blocks write it,
        # consumer blocks read it. It is shared by reference across all backends in this span so a donor
        # and a consumer hosted on the same server interoperate without touching the wire. When a donor
        # lives on an upstream server, the client ships its K/V here to seed the dict; the keys we did
        # NOT receive but produce ourselves are emitted back for downstream consumer spans.
        shared_kv_keys = step_metadata.get("shared_kv_keys", [])
        if shared_kv_keys:
            seed_tensors = [t.to(requested_backends[0].dtype) for t in shared_kv_input_tensors]
            shared_kv_states = unflatten_shared_kv(shared_kv_keys, seed_tensors)
        else:
            shared_kv_states = {}
        seeded_kv_keys = set(shared_kv_states)

        merge_max_tokens = MAX_NF4_SHORT_INFERENCE_TOKENS if quant_type == QuantType.NF4 else MAX_SHORT_INFERENCE_TOKENS
        can_merge_pools = batch_size * length_increment <= merge_max_tokens
        priority = prioritizer.prioritize(
            hidden_states,
            hypo_ids,
            points=point_per_piece,
            requested_uids=requested_uids,
            type="inference",
        )

        # KV-sharing donor K/V this span produces for downstream consumer spans. The workers mutate
        # `shared_kv_states` in their own process, so the produced K/V comes back through the task-pool
        # return channel (trailing tensors after the hidden states), not the handler-side dict.
        produced_shared_kv = {}

        # A client may pass a tensor with 0 tokens. This is a special case that occurs, e.g.
        # when user wants to pre-allocate cache or check that server *can* allocate that cache.
        if hidden_states.numel() > 0:
            assert hidden_states.ndim == 3, f"hidden states must be a single 3d tensor"
            if can_merge_pools:
                inference_infos = tuple(
                    InferenceMetadata(
                        uid,
                        prefix_length,
                        tuple(handles),
                        active_adapter,
                        per_layer_input=per_layer_input,
                        shared_kv_states=shared_kv_states,
                    )
                    for uid, handles, per_layer_input in zip(requested_uids, cache_handles, per_layer_inputs)
                )
                hidden_states, *produced_tensors = await requested_backends[0].inference_pool.submit_task(
                    hidden_states, hypo_ids, inference_infos, *prompts, priority=priority
                )
                # The merged step returns produced donor K/V in sorted layer-type order across the span.
                span_donor_types = sorted(
                    {lt for backend in requested_backends for lt in backend.donor_layer_types} - seeded_kv_keys
                )
                produced_shared_kv = unflatten_shared_kv(span_donor_types, list(produced_tensors))
            else:
                for backend, uid, handles, prompt, per_layer_input in zip(
                    requested_backends, requested_uids, cache_handles, prompts, per_layer_inputs
                ):
                    inference_infos = (
                        InferenceMetadata(
                            uid,
                            prefix_length,
                            tuple(handles),
                            active_adapter,
                            per_layer_input=per_layer_input,
                            shared_kv_states=shared_kv_states,
                        ),
                    )
                    hidden_states, *produced_tensors = await backend.inference_pool.submit_task(
                        hidden_states, hypo_ids, inference_infos, prompt, priority=priority
                    )
                    backend_donor_types = [lt for lt in backend.donor_layer_types if lt not in seeded_kv_keys]
                    produced_shared_kv.update(unflatten_shared_kv(backend_donor_types, list(produced_tensors)))

        # serialize and send last layer outputs
        output_tensors = [
            serialize_torch_tensor(result.to(proto.dtype), proto.compression, allow_inplace=True)
            for result, proto in zip((hidden_states,), nested_flatten(requested_backends[-1].outputs_schema))
        ]

        # Append the donor K/V this span produced (keys it did not receive from upstream) so the client
        # can carry them to downstream consumer spans. One donor per layer type exists in the whole
        # model, so a pass-through span produces nothing and adds no wire traffic.
        response_metadata = {}
        if produced_shared_kv:
            produced_keys, produced_kv_tensors = flatten_shared_kv(produced_shared_kv)
            output_tensors.extend(
                serialize_torch_tensor(t, runtime_pb2.CompressionType.NONE, allow_inplace=False)
                for t in produced_kv_tensors
            )
            response_metadata["shared_kv_keys"] = produced_keys

        can_push = not has_prompts and not has_per_layer_inputs and not seeded_kv_keys and not produced_shared_kv
        yield output_tensors, can_push, step_metadata, response_metadata

        # prepare for next step
        prefix_length += length_increment
