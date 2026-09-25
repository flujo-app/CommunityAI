# Bounded local vLLM smoke checkpoint

Status: **prepared, not run**. A real vLLM process and CommunityAI-to-vLLM
completion have not yet been observed on this host.

The local RTX 2070 SUPER was visible inside Docker, and the existing
Qwen3-1.7B snapshot has its config, tokenizer and two weight shards. The
standalone `scripts/smoke_managed_vllm_local.py` uses an amd64 digest of
`vllm/vllm-openai:v0.30.0`, mounts only that model's cache read-only, disables
Hub access and vLLM usage-stat uploads, publishes only a loopback port, and
removes its container after a single bounded readiness/model/version and
streaming-usage check. It does not start until that pinned image is cached.

The image manifest reports **8,730,525,875 compressed bytes** across its
layers. Three bounded pull attempts were stopped before the image was cached.
On September 25, a three-minute attempt completed several layers; a later
ten-minute attempt reused some of them but went quiet on larger remaining
layers. After each stop, `docker image inspect` returned `No such image`, and
there was no remaining pull process or smoke container. No weights were
downloaded and no GPU inference ran. These attempts are not a vLLM pass or a
benchmark. Further pulls need a credible transfer window or an already-cached
image. The pinned image is separate from the DeepSeek nightly candidate and
cannot qualify either exact requested model.

The local source launch specs now set `VLLM_NO_USAGE_STATS=1` alongside offline
Hub/Transformers mode. [vLLM v0.30.0 environment documentation](https://docs.vllm.ai/en/v0.30.0/configuration/env_vars/)
defines that setting. The focused adapter and DeepSeek launch checks pass in
seconds. A real backend smoke, multi-GPU run, supervised lifecycle and release
package evidence remain open.
