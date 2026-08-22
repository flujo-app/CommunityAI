# Petals revival: inference-first plan

This repository starts from DRIFT-LLM, the most practical maintained continuation
of Petals found during the August 2026 fork audit. It preserves the parts that are
most valuable for a revival: transformer-block sharding, Hivemind DHT discovery,
fault-aware routing, heterogeneous devices, and a standard OpenAI-compatible API.

The original Petals 2.2.0 source snapshot remains separate and unchanged. The
following remotes are configured in the local revival checkout:

- `origin`: the writable [`flujo-app/CommunityAI`](https://github.com/flujo-app/CommunityAI)
  revival fork;
- `drift`: the working codebase used as our starting point;
- `upstream`: the original BigScience Petals repository;
- `nakshatra`: an active, independent llama.cpp/GGUF distributed-inference effort
  that we will track for discovery, transport, and reliability ideas.

## Scope

Inference is the product. Distributed training and fine-tuning are compatibility
features, not roadmap priorities. Near-term work must make it easy for ordinary
computers to contribute model blocks and for clients to receive correct, streamed
responses.

## Milestones

1. **Reproducible execution baseline.** Native Windows, Linux, and macOS setup;
   local DHT smoke test; exact token parity with a stock model; CPU and accelerator
   diagnostics. Windows CPU, Linux CPU, and Windows CUDA are proven on an
   eight-block TinyLlama swarm; macOS remains outstanding.
2. **Real multi-machine swarm.** Two or more machines, explicit block coverage,
   restart testing, disconnect recovery, and latency/throughput measurements. A
   private Fly Machines swarm has proven coverage, redundancy, exact parity,
   measurements, and restart recovery. Exact full-prefix activation replay now
   recovers an interrupted local two-replica swarm with token parity. Repeating
   the original in-generation SIGKILL test on a rebuilt Fly swarm is the remaining
   Milestone 2 confirmation.
3. **Safe public pilot.** Signed worker identity and announcements, admission and
   rate limits, health/coverage monitoring, abuse controls, and documented prompt
   visibility. A private/VPN swarm remains the default until these gates pass.
4. **Volunteer usability.** One-command worker installation, automatic model/block
   selection, resource budgets, background service integration, and contribution
   accounting.
5. **Public service.** Redundant bootstrap peers, an OpenAI-compatible gateway,
   capacity-aware routing, observable service-level objectives, and contributor
   access credits.

Detailed baseline evidence and the current blocker are recorded in
[`REVIVAL_TEST_RESULTS.md`](REVIVAL_TEST_RESULTS.md).

## Nakshatra relationship

Nakshatra is promising but is not a drop-in Petals fork: its active engine is a
patched llama.cpp daemon using sliced GGUF files and a gRPC chain, while its copied
`petals` package is largely historical. Its signed listings, public discovery,
transport experiments, layer-package distribution, and recovery work are useful
design references. Direct code merging is unlikely; ideas should be ported behind
small interfaces and verified against this repository's end-to-end inference path.

## Release gates

No public-swarm release is complete unless all of these are demonstrated:

- distributed output parity against a stock reference model;
- full block coverage with at least one redundant route;
- recovery from a worker disappearing during generation;
- bounded memory and attention-cache cleanup;
- authenticated worker metadata and encrypted transport;
- explicit warning that volunteer workers may observe or retain request data;
- an automated health view that does not depend on a single host.
