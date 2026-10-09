# Confidential execution for FACTORY CommunityAI and O

The goal is to run FACTORY software agents confidentially on Modal if its platform can be qualified, and share the admission design with CommunityAI and O. As of October 3, 2026, Modal is **unqualified for Protected execution on the examined public evidence**. A private offering remains unknown. The portable policy source described here is an offline proposal; it neither provides hardware attestation nor enables confidential execution in any running product.

## What Modal evidence establishes

Modal documents gVisor and KVM isolation. Its GPU Sandboxes currently require the gVisor runtime. These statements do not establish a confidential CPU guest or an attested CPU/GPU execution path. [Sandbox runtimes](https://modal.com/docs/guide/sandboxes), [security model](https://modal.com/docs/guide/sandbox-networking).

Modal documents encryption in transit and at rest, while inference endpoint TLS terminates at its edge proxy. Function arguments/results, logs and snapshots also have retention rules. Our inference is that these documented controls alone cannot establish provider-blind plaintext processing. [Modal security and retention](https://modal.com/docs/guide/security).

No examined product documentation supplied a customer-selectable TDX/SEV-SNP profile and independently verifiable workload-bound CPU/GPU attestation. This is a qualification gap, not proof that Modal can never support it. H100 availability, a CC-related GPU health error, OIDC identity, a successful health check, a VM flag and a zero-retention policy cannot substitute for that evidence.

## Current product boundaries

| Product | Existing controls and plaintext recipients | Confidential work still needed |
| --- | --- | --- |
| FACTORY | Original spending and OFF gates, private credentials, TLS, snapshots and source/model pins. Worker/provider paths receive plaintext. | Admit the exact worker and inference endpoint before repository, prompt or credential access; protect recovery and approved updates. |
| O | Account tokens remain on the trusted host; generated code uses separate tools Sandboxes with blocked network. The host assembles prompts, mediates web fetches and persists checkpoints. | Cover model and tools separately; pin model weights; define host trust and protect outputs/checkpoints when that host is excluded. |
| CommunityAI | Existing B4 roadmap requires a complete-model confidential CPU/GPU worker before expanding the fleet. | Supply real composite evidence verification, an attested encrypted session, admitted artifacts and protected reconnect. |

O's details come from OWNER's review of its qualified `proposals/cloud-conversation-intents-v2/candidate/server/modal_runner.py`, `execution.mjs`, `mission.mjs` and `modal_verify.py`. FACTORY's owner confirmed no qualified TEE evidence in its current runtime. These are source/owner findings, not observations of live protected hosts.

Protecting inference alone does not protect a tools worker that receives plaintext files. Each developer, reviewer and tool path receiving private material needs its own covered execution profile. External LLM, MCP, Git and web services remain recipients of whatever the admitted agent sends. An approved egress policy must name or forbid those disclosures. A model opinion provides no security assurance.

The initial scope may trust the user's client/control host while excluding provider operators and hypervisors. Excluding that control host requires a larger redesign: prompt assembly, credential release, private state and sensitive orchestration must move into the protected boundary. The current products do not meet that stronger scope.

## Portable admission proposal

[`scripts/confidential_compute_admission.py`](../scripts/confidential_compute_admission.py) uses the Python standard library so O can import it and FACTORY can review the same semantics for its host adapter. It has **no installed hardware verifier and no qualified provider profile**. Its default rejects Modal and every other provider for required-confidential work.

The binding covers provider and versioned profile identity, factory, namespace, principal, original job/account/role, attempt, commitment and reservation, request digest, session, audience, fresh challenge, policy epoch/digest, source graph, workload, tools and egress policies, model artifact, endpoint key and original lease expiry. Separate profiles can describe different CPU-tools and GPU-model platforms under the same provider. The newly versioned request digest must include its confidentiality requirement; an existing request UUID cannot acquire a different policy silently. The host supplies these identities from the original authoritative records.

`preflight` rejects unknown providers/classes before sensitive payload loading or paid provisioning. After an exact provider configuration is qualified, public bootstrap provisioning may precede attestation, but every billable operation still needs the original paid admission and effect journal. A provisioned resource with failed attestation retains its original identity, uncertain outcome and held commitment until owned cleanup/billing is reconciled.

Installing any profile requires an explicit policy/revocation authority callback; missing or unavailable authority refuses admission. The host must establish the callback's authenticity and liveness; merely supplying a constant function proves neither. `admit` calls a host-configured verifier and checks its internal appraisal against the complete binding, pinned verifier/platform, current authority generation, freshness, collateral expiry and bounded original lease. GPU work additionally requires a protected CPU and the adapter's verified CPU/GPU association. CPU tools require their own CPU profile. The adapter must verify signatures, roots, TCB/revocation, measured artifacts, actual GPU assignment and guest-held key possession using reviewed upstream components.

An `Appraisal` is an internal Python value returned by that trusted adapter. It is **not** a cryptographically authenticated or transferable grant. Worker JSON such as `verified=true` is rejected. Installing a callback that manufactures this value does not make a provider confidential. No production adapter, cryptographic verifier or authenticated grant transport is implemented here. Challenge generation/replay tracking, trusted clocks with monotonic elapsed deadlines, raw evidence verification, session-key release and key destruction remain adapter obligations. Calling the adapter repeatedly does not itself prove a fresh hardware quote.

The synchronous `dispatch` example preflights every covered worker and applies existing authorization before appraisal can invoke a verifier. It reauthorizes and checks expiry/revocation before preparing payloads. It repeats authorization, appraisal and final checks before sending. Existing budget, OFF, writer, capture and provider-role controls remain mandatory. Every potentially billable verifier operation also requires the original spending authority. FACTORY/O asynchronous transports need equivalent checks after every await and at actual key release/send; this helper is not an atomic distributed transaction. Required-confidential work has no ordinary, local, other-account or other-model fallback.

The helper rejects direct async/generator callbacks and known deferred results, including wrapped coroutines, generators, async generators and futures. Rejection cannot undo side effects that a callback already scheduled or started, nor identify every custom lazy object. The host adapter must bind an actual verified session and perform checks inside its real encrypted transport rather than returning a deferred send from this helper.

The host must derive a complete protection plan from the approved data-flow graph. Profiles pin admitted audiences, and duplicate session requirements are refused. This module cannot detect an omitted worker or prove that `send` uses an attested encrypted channel. The transport and upstream key broker must enforce those properties. Cleanup and recovery reads remain possible; admission is no proof of termination, spending availability or refund.

## Upstream components to evaluate

| Component | Intended reuse | Qualification limit |
| --- | --- | --- |
| [Confidential Containers Trustee](https://github.com/confidential-containers/trustee/tree/3b7c99069a7c89ea51713dcf7cf98c16dbe2d3db) | Hardware evidence appraisal, reference values and conditional secret delivery. | Workload, appraisal and resource policies must agree. Defaults alone do not admit our workload. |
| [Guest components](https://github.com/confidential-containers/guest-components/tree/17ad60d88f9b7e4b3b54d01200985ae72723e8ab) | Guest attestation agent and confidential data access. | Validate measurements and actual CPU/GPU assignment on one exact supported platform. |
| [NVIDIA nvtrust](https://github.com/NVIDIA/nvtrust/tree/858ada9a17f58c482f578414ea2455498fa51e17) | Evaluate vendor GPU evidence tooling alongside the CPU verifier. | GPU evidence alone cannot protect ordinary host memory or tool execution. |

These are research pins from `git ls-remote HEAD` on October 3, not approved releases or adopted dependencies. Trustee can serve different workloads and deployment shapes; a one-worker prototype does not require Kubernetes solely to host its key broker. [Trustee architecture](https://confidentialcontainers.org/docs/attestation/), [three policy layers](https://confidentialcontainers.org/docs/attestation/policies/).

First implementation target: integrate the default refusal and versioned binding into a separate FACTORY/O source proposal, then attach one reviewed verifier and encrypted-session adapter on a supported platform. Keep vLLM/Ollama as explicitly measured workloads rather than allowing arbitrary runtime adapters. The attested channel for user prompts is a separate requirement from model-weight key release.

If Modal cannot qualify, a possible alternative is Modal carrying opaque encrypted work envelopes to independently attested workers elsewhere. Execution would take place on that other provider. This does not make existing Modal workers confidential, and FACTORY/O currently require changes to avoid plaintext orchestration there.

## Qualification and provider questions

Before any hardware run, establish the exact CPU/GPU/firmware/driver/runtime tuple, hardware mode selection API, evidence format, endorsed roots, revocation/collateral freshness, measured workload policy, guest-held endpoint key and protected CPU/GPU connection. Verify how logs, debugging, state snapshots, migrations, egress and key deletion behave. For a CPU tools profile, GPU evidence is unnecessary; for inference it is essential. No provider contact has been sent.

Ask Modal whether it offers these controls to customers, whether confidential guests can receive supported GPUs, how clients independently verify quotes, whether the proxy can remain an opaque carrier of application ciphertext, and which data/metadata remain exposed. Obtain a documented contract and sample evidence without sending private repository data or credentials. A provider statement still needs independent runtime verification.

Offline tests must reject stale/replayed or malformed evidence, substituted endpoints, mismatched artifacts/policies, unrelated GPUs, wrong account/job/role, revocation, expired collateral, lease extension and ordinary fallback. Only after exact platform eligibility and original spending authority exist should a bounded synthetic job test actual encrypted prompt delivery, tool paths, logs/snapshots, revocation and replacement. No paid job has been started for this work.

## Evidence and coordination

The source-only admission proposal passed **32 offline unittest cases**, including per-field identity substitution, default Modal refusal before any payload or paid callback, separate worker coverage, CPU/GPU distinctions, original lease preservation, revocation/expiry during preparation and authorization, and OFF/budget refusal before any verifier call. Additional regressions cover different profiles under one provider, deferred callback results, pinned audiences and duplicate session requirements. All acceptance fixtures are synthetic. Run `python -m unittest discover -s tests -p test_confidential_compute_admission.py -v` from the repository root.

The researcher independently verified the earlier 21-test proposal and identified authorization ordering, profile identity and deferred-callback gaps. Those findings drove this revision. Exact earlier source, manifest and test evidence remain archived under `.communityai-beta/confidential-computing/v1/`; the current working files are a new generation.

FACTORY, the confidential-computing researcher and OWNER exchanged requirements under the user's coordination instruction. Product runtime adoption remains separate from this proposal. The capability snapshot is [`config/confidential_compute_capabilities.json`](../config/confidential_compute_capabilities.json).

The requested FLUJO model was resolved from UUID `42a596b8-ff78-4d69-b5c7-79dd3ce2bb1e` to its advertised direct-completion alias `model-Claude Opus`. An initial UUID identifier received `model_not_found`; the corrected selected-model request returned HTTP 200 containing an OAuth token-revoked failure, with zero reported tokens. After human reconnection was reported and the selected identity was rechecked, a new supplied-context request obtained an architecture/code opinion through the same FLUJO API at Medium effort with no tools. Earlier failures remain retained; no fallback model or credential mutation occurred in this chat.

The FLUJO reviewer recommended a CPU-tools TEE/key-release prototype first, separate assurance for each path, an opaque attested-session handle, and real checks at encrypted send. Its code comments led to the pinned-audience and duplicate-session regressions. It reviewed the preceding 25-test source, so its opinion is not acceptance of the final 32-test bytes. The reviewer also overstated two points: public gaps cannot prove categorical Modal impossibility, and public model weights need integrity validation without necessarily requiring secrecy. O's local control host is explicitly trusted in the initial scope; a claim excluding that host needs a different architecture. Model output is advisory, not evidence of hardware protection.

Local review request/response hashes, failure and success receipts, and test logs are retained under `.communityai-beta/confidential-computing/`, which is ignored by Git. The successful response SHA-256 is `d32e566973680395789c8037161a9d236aba9fde3b94a4c7aae59b760b9d5120`. The selected model configuration itself was not archived, and no credentials were copied into the evidence. The goal remains open pending real provider qualification and upstream adapter/transport integration.
