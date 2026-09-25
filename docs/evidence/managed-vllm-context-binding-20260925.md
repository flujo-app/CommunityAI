# Managed vLLM context binding — September 25, 2026

Status: source contract check passed; no vLLM image or exact requested model was
executed in this check.

`ManagedVllmBinding` now carries a single validated context envelope. Its
launch command and runtime probe reject a different length, and the existing
text bridge uses the same value when constructing typed inference limits. This
prevents a 256-token server from being routed with the bridge's former default
2,048-token assumption. The default remains 2,048 for existing test bindings.

The standalone `scripts/check_vllm_context_binding.py` failed before the
change on the missing binding field, then passed in under a second. It verifies
matching launch arguments, rejects a mismatched launch/probe, and rejects an
output request at the served context limit before backend dispatch. Existing
managed vLLM adapter, probe, owned-process and API checks passed. The pinned
DeepSeek-V4.1-Flash and GLM-5.3 H100 candidate launch checks also passed as
source-only fixtures. Their actual profiles remain unavailable until real
weights, runtime, hardware, rights and product-path qualification pass.
