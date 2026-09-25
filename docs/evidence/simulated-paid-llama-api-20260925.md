# Simulated credits bound to a managed text request — September 25, 2026

Status: **local noncash implementation and checks passed**. No live processor,
cash, payout, approved commercial route or production credit balance was used.

`src/drift/simulated_paid_text.py` wraps an admitted synthetic managed text
provider. The API key identifier supplies a buyer ID; an immutable quote binds
that buyer, request, provider, model/profile, artifact digest, rates, output
limit and funding source. The existing SQLite journal reserves the quoted cap
before backend dispatch. Only a typed completed stream with accepted usage
settles the actual charge and fee. The receipt digest commits the quote,
reported usage, finish reason and output hash. Failure or cancellation refunds
the whole hold. Final API completion is sent after the settlement transaction.

The standalone `.gate13-runs/prototype_paid_text_bridge.py` first passed
reserve/settle/error/cancellation behavior in 0.3 seconds. The focused
`scripts/check_simulated_paid_text.py` then passed through the authenticated
CommunityAI `/v1/completions` route with a fake local backend. It checked
unauthorized and oversized requests did not hold funds, one accepted usage
charge moved 3 test units from a buyer's 100-unit purchase to provider pending,
and malformed or cancelled streams refunded their holds. It completed in about
8 seconds, including the API test-client startup.

`scripts/check_managed_llama_cpp_process.py` repeated one paid API request
against the actual pinned llama.cpp b11173 CUDA process and cached Qwen3-1.7B
GGUF on an RTX 2070 SUPER. The response succeeded, the buyer balance fell by
its reported input plus output units, provider pending increased by charge
minus the test fee, no service hold remained, and journal audit reported no
unfunded reversal loss. This script also confirmed pinned artifact hashes,
owned listener admission and cancellation process-tree teardown. The whole
standalone process check finished in about 25 seconds; no server remained.

The 0.3-second `.gate13-runs/prototype_orphan_hold.py` first established that a
reserved hold survives close/reopen. `unresolved_service_holds(limit=...)` now
returns bounded, canonically checked quotes for recovery. The focused
`scripts/check_abandoned_service_holds.py` passed crash/reopen inspection,
explicit refund, and a concurrent settlement/refund race in under a second.
The journal does not refund merely because time passed: the caller must verify
the provider work stopped or adjudicate an accepted receipt first.

This is a synthetic contract check. The simulated processor callback is assumed
verified, and the stream's accepted usage stands in for independent work
validation. There is no payment-account integration, funding reconciliation,
approved provider compensation, seller eligibility, payout or credit-resale
activation. A crash after reservation still needs an external process-stop or
receipt decision before recovering an unresolved hold; this bridge handles
ordinary exception and cancellation cleanup but does not supply that
adjudication service. None of these test units represent money.
