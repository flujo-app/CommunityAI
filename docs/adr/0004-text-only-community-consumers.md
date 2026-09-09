# ADR 0004: Community consumers send text; contributors own all model stages

- Status: Accepted; source implementation and short live requests verified
- Date: 2026-09-09
- Supersedes: ADR 0001's requirement that the consumer owns tensor routing

## Product behavior

Open the app and use the LLM. Sharing hardware is optional. A consumer must not
need a GPU, a tokenizer, input/output model weights, or a model download to use
a complete community model.

Automatic selection prefers the community model when the mesh can answer a
complete request. If it cannot, the existing small local Qwen fallback remains.
When community capacity returns, subsequent requests use the community again.
Explicit local-only selection remains available. An answer already in progress
is never assembled from different models.

Sharing budgets are independent of fallback. A 100% GPU-memory setting offers
the full physical GPU memory budget to the network; configuring a local fallback
does not subtract a permanent reserve. Local execution checks actual free memory
when needed and again after downloads. Automatic local device selection uses
CPU/RAM when GPU memory is insufficient.

## Decision

Contributors can serve transformer blocks and a text-service role. The text peer
owns tokenization, input embeddings, output projection and the generation loop;
its transformer execution uses the existing distributed block route. Consumers
send chat/completion requests and receive text and usage over the existing
authenticated libp2p transport. No central HTTP inference gateway is introduced.

Text-service announcements are signed by the transport identity and bind the
exact manifest, execution profile, protocol, context/output limits and expiry.
They expire after 40 seconds and are published only while the provider observes
a complete block route. Discovery reports block coverage and chat availability
separately. A green block grid alone is insufficient to claim a usable model.

The product node uses the text client for community inference. The existing
tensor client remains an internal building block for text peers and the advanced
`drift api` command. It must not be used for desktop community consumers.

Requests, frames, answers, context and generation time are bounded. A peer admits
one generation at a time. Cancellation is tied to the requesting transport
identity and keeps the generation slot occupied until the computation stops.
Another text peer may be tried before answer text begins; after that a connection
failure is reported without splicing another answer into the same stream.

## Deployment and limits

`drift text-peer MANIFEST --initial_peers ... --identity_path ... --cache_dir ...`
starts a dedicated contributor role. It needs memory and storage for input/output
weights in addition to any blocks it serves. Ordinary consumers need neither.
The CLI currently enables this role explicitly; automatic placement and resource
accounting of text roles in desktop sharing remain follow-up work.

Block availability, text-service availability and spare generation capacity are
different observations. A signed advertisement cannot guarantee the next request
will succeed. Peer failure can still produce a retryable error during an answer.
Expiry removes a lost text service from subsequent automatic selection.

The local fallback still downloads and executes its own small model when needed.
This decision removes community weight downloads from consumers; it does not
make the local fallback run remotely or establish a new minimum system requirement.

[September 9 live evidence](../evidence/text-only-mesh-consumer-20260909.md)
records short completion and chat responses from fresh clients with artifact
downloads forbidden. Packaged delivery remains separate.
