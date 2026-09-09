# Qwen chat prompt transport repair — September 9, 2026

The reported Goose greeting includes a system prompt. Its chat template produced
533 tokens, corresponding to 5,457,920 bytes of BF16 hidden states before protocol
metadata. Public worker inference messages are bounded to Hivemind's 4 MiB limit.
The worker correctly rejected the oversized message; sending an ordinary prompt
as a single message was the client-side generation error.

The text peer now uses 64-token prefill chunks, preserving the full prompt and
remote inference cache. It also admits one running generation and at most two
queued requests, so an answer and a background conversation title can coexist.
Queued requests remain cancellable and receive heartbeats. A forward pre-hook also
checks cancellation between prefill chunks, where Transformers does not check its
decoding stopping criteria. Thinking is disabled
by default on this text service and can be requested explicitly.

The first live chunked request exposed a second issue: each Qwen block owns only
its own cache, but Transformers' causal-mask helper selected the first full
attention layer in the model-wide cache. At later full attention layers, that
entry was empty. The second prefill chunk then had 128 keys and a 64-key mask.
Mask creation now receives a view of the current block's cache.

The first complete A100 worker loaded all 64 blocks but exited with SIGFPE while
constructing the input/output runtime. A separate CPU-only Modal subprocess
reproduced SIGFPE merely by importing `cpufeature`. The language-model head now
uses PyTorch's BF16 capability check, with a conservative fallback when the probe
is unavailable. Constructing that head on Modal then succeeded (exit code 0).

Validation:

- The eight-layer, 533-token regression reproduced the live 128-versus-64 error
  before the mask fix.
- After the fix, chunked distributed generation produced the same generated
  tokens as the reference model and retained every prompt token.
- Focused tests passed across text generation, Qwen block/cache behavior,
  authenticated text transport, cancellation between prefill chunks, and head
  construction without importing `cpufeature`. The transport case also checks
  that a peer's bounded error message reaches the consumer.
- The original 4 MiB admission bound and signed model/peer checks remain active.

Live rollout and handover evidence is retained in the private operator directory
`.gate13-runs/modal-live-20260909/`. Raw request/response logs are not published.

The first GPU greeting began streaming after 17.11 seconds, but continued into an
invented conversation turn and reached the 128-token output cap. The tokenizer's
chat end token (`248046`, `<|im_end|>`) differed from the model's end-of-text token
(`248044`). Chat generation now stops on either token. A focused regression
checks that both stop IDs reach generation. This first live response did not
qualify for the cloud cutover; clean, naturally terminated replies are required.

The post-mask-fix CPU run did not produce greeting text within the consumer's
900-second limit. The temporary CPU fleet's latency remains a separate limitation;
passing the small numerical tests is not evidence of usable CPU response time.

The corrected Modal container's SHA-256 hashes match the local source overlays
for text generation, the Qwen block wrapper, and the language-model head. The
source repairs are committed as `540035f` and `40b9e53` on
`codex/gate14-20260902-b`. These are contributor/runtime changes; the installed
Windows binaries were not replaced, and the desktop window stayed open.

## Verified deployment

The complete 64-block model is running on one Modal A100 80 GB. The integrated
text role loaded its weights but failed to establish a usable discovery route.
Its underlying transport could ping the bootstrap successfully; the precise
cause of that role's discovery failure is not yet isolated. A separate text
process in the same container, seeded through the local block worker and pinned
to its signed identity, passed both real requests. This preserves the model
already loaded on the GPU.

Moving the text process's input/output weights onto the A100 also removes the
CPU projection bottleneck. The generation engine now places input tokens on the
model's device. With this role, the original Goose greeting (497 prompt tokens,
thinking disabled) began returning text in 8.33 seconds and finished naturally
in 20.14 seconds with 60 output tokens. The queued title also ended naturally.
The worker log confirms an authenticated inference session covering blocks 0:64.

With the temporary GCP workers stopped, the installed client's normal API and
`model="auto"` selected `Qwen3.8 27B FP8 Dequant`. Both requests returned HTTP 200,
streamed text, and ended with `finish_reason="stop"`. In that run, the title
finished in 12.78 seconds; the greeting finished in 25.70 seconds including its
wait behind the title. These checks validate text chat, not Goose tool calling.

## Discovery cache failure during handover

Stopping the CPU fleet exposed stale cached seed addresses in the installed
client. Its discovery process repeatedly failed to restart. Using the original
six-address set reproduced a 15.05-second startup timeout, while using the
configured bootstrap alone succeeded in 3.69 seconds. The new shared client-DHT
factory attempts seeds separately and closes failed attempts. With the same six
addresses, the repaired factory connected in 3.78 seconds. Focused regressions
also cover falling back when the first configured seed is down.

For the running client, obsolete CPU peer hints were removed and only its owned
background node was restarted by the existing desktop supervisor. The desktop
window and installed version remained unchanged. The permanent factory repair
is source code for the next desktop update; the installed v3 binary does not yet
contain it. The final installed-client results above were recorded after this
reconnection. Focused discovery, transport, generation and desktop-contract tests
passed; one test initially imported an older editable desktop checkout and passed
when rerun with this checkout's desktop source on `PYTHONPATH`.

## Timed cloud handover

All four temporary GCP VMs were confirmed stopped. CPU0 was resized to
`e2-highmem-16` (16 vCPUs, 128 GiB), matching the combined CPU and RAM of the four
original workers, and its complete 73-artifact model cache is verified. Its
full-model text role is pinned to its own blocks. A native GCP schedule starts
only CPU0 at 2026-09-09 16:30 UTC. Full-model CPU response time still needs the
scheduled live check; the earlier four-worker CPU greeting exceeded 900 seconds.

The Modal worker and both separate text processes use the original absolute
deadline, 2026-09-09 17:20:48 UTC. The account's paid-spending limit remains $0.
All four temporary GCP VMs retain native automatic deletion at
2026-09-10 05:57:40 UTC. The permanent bootstrap was inspected but not changed.
The handover automation checks both the block worker and the active `text_cuda`
role, verifies CPU recovery before GPU shutdown, and preserves those deadlines.

At the final billing check, Modal reported $21.16 usage, all covered by credits,
and a $0 paid-spending cap. The CPU restart was advanced by 20 minutes because
the remaining monthly credits may be exhausted before the fixed GPU deadline.
The GPU may therefore stop earlier under the account's spending cap.
