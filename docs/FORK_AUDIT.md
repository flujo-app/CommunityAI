# Petals continuation audit

Audit date: 2026-08-21

Selected base commit: `dd294a15da3b3b9c61c51e6bf36d58696759cec9`

## Decision

Use [ApexDevelopment/DRIFT-LLM](https://github.com/ApexDevelopment/DRIFT-LLM)
as the code base for the inference-first Petals revival. Track
[fthrvi/nakshatra](https://github.com/fthrvi/nakshatra) as a separate experimental
engine and design reference, not as a branch to merge wholesale.

This decision is about the shortest credible path to useful execution. It can be
revisited if another continuation demonstrates better end-to-end reliability.

## Method

The upstream fork network was enumerated through the GitHub API. Recent push dates
were not treated as proof of maintenance: several apparently active forks had no
tree or commit difference from upstream. The viable candidates were compared by:

- actual commit and tree divergence from upstream;
- preservation of Petals' DHT, block routing, and client API;
- model and dependency modernization;
- installation and operator experience;
- automated tests and recent workflow results;
- public-versus-private networking readiness;
- a native Windows test on this machine where practical.

## Candidate findings

### DRIFT-LLM — selected base

DRIFT is a direct continuation of the Petals architecture. It retains Hivemind DHT
discovery, transformer-block serving, remote sequential routing, and the
Transformers-style client while adding a cluster CLI and OpenAI-compatible API.
Its current code supports recent Llama, Qwen, Gemma, Mistral, Mixtral, DeepSeek,
Falcon, and BLOOM families. At audit time it was 83 commits ahead of upstream with
188 changed files, and its latest GitHub Actions run passed.

The important limitation is intentional: DRIFT currently targets private clusters.
Public volunteer operation still needs identity, abuse controls, encrypted
transport, health monitoring, capacity-aware block placement, and failure testing.

### Nakshatra — promising adjacent project

Nakshatra's active runtime is not the Petals runtime. It uses a patched llama.cpp
daemon, sliced GGUF files, gRPC chaining, signed manifests/listings, and newer
discovery experiments. The repository contains substantial independent work—about
353 changed files and 62,000 changed lines relative to the upstream Petals tree—but
its new execution path is mostly exposed through scripts rather than a cohesive
installed package or operator CLI.

A focused native-Windows run of 20 new Nakshatra test modules produced 479 passes,
1 skip, and 8 failures. The failures included POSIX permission assumptions, a CRLF
private-key parsing bug, a Windows HTTP-server abort, and a stale gRPC message-size
expectation. There were no GitHub Actions runs to establish a second environment.
That is a respectable research baseline, but not yet the lower-risk foundation for
a public volunteer service.

Useful ideas to evaluate independently include its signed listings, public
discovery, layer-package distribution, transport experiments, and recovery work.

### Other continuations

- `dkanda/petals` had significant commit divergence, but much of it was test/agent
  churn, its current Actions runs failed, and the runtime dependency stack remained
  close to old Petals.
- `helion-network/helion-core` contained Qwen 3 work, but documentation and current
  implementation status did not line up cleanly and it had no active CI evidence.
- Several recently pushed forks were byte-for-byte or commit-for-commit equivalent
  to upstream and therefore did not represent maintained alternatives.

## Local baseline established

The selected base was cloned into a new checkout so the supplied Petals 2.2.0
snapshot remains unchanged. The revival branch has three read-only lineage remotes:
`drift`, `upstream`, and `nakshatra`, plus the writable revival fork at
[`flujo-app/CommunityAI`](https://github.com/flujo-app/CommunityAI).

On native Windows with Python 3.12 and CPU PyTorch:

- the patched Hivemind Windows wheel built and installed successfully;
- the exact offline CI suite passed: 100 passed, 7 skipped;
- an eight-block TinyLlama DHT/server/client swarm started and shut down cleanly;
- the client discovered a complete `0:8` route and performed RPC inference; and
- distributed greedy generation exactly matched the stock Transformers model.

This proves a reproducible one-machine execution baseline. It does not yet prove a
multi-machine swarm, CUDA execution, disconnect recovery, or safe public service;
those remain explicit milestones in `docs/REVIVAL.md`.
