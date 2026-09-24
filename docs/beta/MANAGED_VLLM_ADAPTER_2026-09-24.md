# Managed vLLM adapter checkpoint

Status: **source implementation and simulated transport verification**, not a
qualified vLLM service, exact-model support, or multi-GPU result.

`src/drift/managed_vllm.py` adds a bounded local `/v1/completions` streaming
adapter for the typed provider contract. The pinned candidate backend identity
is vLLM `v0.30.0`. A local binding fixes the served model, provider profile,
loopback endpoint and selected device geometry (`tensor_parallel_size ×
pipeline_parallel_size == selected GPU count`). It does not launch the backend,
inspect actual devices or prove that its running configuration matches the
binding. A deployment admission check must establish those facts before use.
For a synthetic qualified test profile, `launch_spec` builds an argv tuple for
one managed host with selected `CUDA_VISIBLE_DEVICES`, matching TP/PP flags,
an explicit 2,048-token-or-smaller context envelope and disabled request
logging. It keeps the API key in an environment override instead of argv.
Artifact verification and process supervision remain with the caller; no
command is executed by this method. Unavailable exact models cannot obtain a
launch spec.
The module also imported successfully through the repository's existing Python
3.12 test environment with its local dependency overlay and offline flags;
this import does not launch vLLM or prove an installed-package workflow.

The adapter sends one prompt with `stream_options.include_usage=true`, accepts
one choice, bounds SSE frames and cumulative output, checks a stable response ID
and exact model name, verifies token totals through the provider contract, and
requires a final usage chunk plus `[DONE]`. Malformed/partial streams fail
without a usage or success claim. The execution deadline is absolute; a timeout
closes the connection but reports **stop unconfirmed**, since HTTP closure is
not proof that vLLM released backend work. Cancellation and cleanup ownership
remain integration work. Only synthetic `test/*` profiles can currently be
available. Both exact DeepSeek-V4.1-Flash and GLM-5.3 profiles refuse before
network access.

The endpoint is restricted to explicit loopback HTTP, with proxy environment
disabled and a managed API key. The [vLLM v0.30.0 server documentation](https://docs.vllm.ai/en/v0.30.0/serving/online_serving/openai_compatible_server/)
warns that its `--api-key` does not protect every inference endpoint, so a
deployed instance also needs the stated network isolation. Its [per-request
metrics documentation](https://docs.vllm.ai/en/v0.30.0/features/per_request_metrics/)
documents the final streaming usage chunk. Its [parallelism
guide](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/) documents
tensor and pipeline parallel serving; that upstream support is not a
CommunityAI multi-GPU PASS.

`scripts/prototype_vllm_sse.py` was run first as a standalone framing
experiment. `scripts/check_managed_vllm.py` then used `httpx.MockTransport`
and chunked SSE to verify accepted output/usage, wrong model, missing terminator,
extra data, wrong usage, unavailable real models, endpoint restriction and
device-geometry rejection, plus launch arguments and key placement. The
focused script completed in under one second on
this checkout; no broad suite or vLLM/model/GPU operation ran.

## Required next work

1. Connect the adapter through authenticated provider discovery and request
   admission. Bind manifest, exact model/profile, service policy, route and
   backend instance. A local model string or HTTP response cannot supply that
   authority.
2. Launch and supervise one pinned vLLM runtime from verified local artifacts,
   with selected GPUs and matching TP/PP configuration. Check runtime/version,
   health, model identity and actual device ownership. Keep Ray/worker control
   traffic on a trusted managed network; raw vLLM endpoints are not public
   volunteer enrollment.
3. Prove server-side stop and resource release on cancellation/deadline, plus
   bounded restart and failure behavior. Usage frames are protocol reports,
   not authoritative credit receipts.
4. Qualify a Qwen baseline, then each exact requested model with its pinned
   prompt encoding/template, rights, model artifacts, supported kernels,
   reference correctness, memory and measured performance on real GPUs.
   DeepSeek-V4.1-Flash custom encoding and GLM-5.3 commercial rights remain
   separate gates. No automatic fallback to an older/smaller name is allowed.

The package's `api` extra includes `httpx` for this adapter. The main legacy
community text path remains separate; no production route is switched here.
