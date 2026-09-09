# Text-only community consumer: September 9, 2026

Two fresh Windows source clients completed real requests through the public
Qwen3.8 27B mesh without downloading model artifacts or loading a local tokenizer
or model. GPU visibility was disabled. The product node's actual model manager,
discovery, auto selection and OpenAI API were used; HTTP was exercised through
ASGI and peer inference used the real encrypted public network.

| Request | Answer | Time | Client cache |
| --- | --- | --- | --- |
| Completion: `The capital of France is` | `Paris.` | 106.94 s | Empty |
| Chat: `What is 2 + 2? Reply with only the number.` | `4` | 157.66 s | Empty |

Both requests used three generated tokens and the exact public manifest
`sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`.
Client process RSS was approximately 569 MiB. This is not a measurement of total
system requirements or all child-process memory. CPU speed is not a performance
qualification. The chat log includes a transient DHT reconnect warning; the
request nevertheless completed through the authenticated provider.

Input embeddings, output projection and tokenization ran on the existing
`qwen-live-0909-cpu0` contributor; all 64 transformer blocks remained distributed
over the four existing CPU workers. The public bootstrap was the only configured
consumer seed. No new paid machines, disks or firewall rules were created.
All four machines retain automatic deletion at **2026-09-10T05:57:40Z**, with
boot disks set to auto-delete.

Focused source checks passed: eight consumer/protocol checks, 46 manager,
discovery and desktop checks, 41 existing API/node checks, and 12 resource-control
checks. These include fallback on incomplete or unavailable mesh, return to the
community, explicit local-only selection, identity/revocation checks, real
encrypted streaming/cancellation, and release of an abandoned API request.

Two implementation failures were corrected before these results: cancellation
used an unsupported unary transport path on the desktop daemon; text-provider
discovery waited for the first inference request and could not advertise initial
readiness. Cancellation now uses the streaming transport and provider discovery
starts before requests are admitted.

Following the owner's clarification, the permanent local-fallback deduction was
removed from hardware reporting and worker admission. A 100% sharing setting on
an 8 GiB GPU now produces an 8 GiB worker budget. Local execution independently
checks free memory and selects CPU/RAM when the GPU is occupied; it rechecks after
downloads before choosing the load device. This follow-up has separate focused
source checks; the public CPU inference results above did not exercise this GPU
budget change.

This evidence covers source clients and the live provider, **not a new packaged
desktop release**. Automatic placement of text roles from the desktop sharing UI
is not implemented; the role is an explicit contributor service.

[Machine-readable results](text-only-mesh-consumer-20260909.json).
