# Shadow credit accounting foundation

Status: **local implementation and simulated verification**, not live credit or
marketplace readiness. Source: `src/drift/shadow_credits.py`.

The stand-alone prototype (`python scripts/prototype_shadow_credits.py`) was run
before the module was added. The focused check (`python
scripts/check_shadow_credits.py`) uses only Python's standard library and took
under one second on this checkout. It covers exact/conflicting replay, hold
overspend, pending receipt refusal, independent-decision digest binding,
settlement with a fee and unused-cap release, rejection/cancellation release,
reopen/audit, and two concurrent holds against one SQLite file. No broad CI,
model download, network call, processor action, or GPU run was used.

## State and accounting

- All balances are integer **test units**. A test grant debits the explicit
  `test_issuance` account; it is the only minting operation. No account can
  withdraw or admit a live paid service through this module.
- A versioned `ShadowQuote` binds buyer/request, exact model and profile,
  artifact and service-policy digests, price-schedule digest, service class,
  settlement domain, input/output rates and limits, maximum fee percentage, expiry and
  spend cap. It is a local data contract, not a signed offer or authorization.
  The stand-alone `prototype_shadow_quote.py` ran first and passed in 0.18 s.
- A new reservation requires that quote and atomically moves its capped amount
  from `buyer:<id>` to
  `hold:<request>`. Concurrent reservations serialize through SQLite's write
  transaction. An exact request/quote replay is idempotent, even after quote
  expiry; changed terms or an expired new quote are rejected. The canonical
  quote and its digest are stored durably and bound to the reserve event.
- A versioned, content-free work receipt is an untrusted claim. It records
  request, provider, stage, attempt, measured units, proposed test charge and
  an evidence digest. It does not contain prompts or outputs. Duplicate receipt
  IDs and repeated provider/stage/attempt claims cannot create another charge.
  New claims must stay within the bound quote's input/output rates and both
  per-claim and aggregate request unit limits. Rejected claims release their
  unit allowance; active pending and approved claims consume it.
- A separate validation decision binds the receipt digest and supplies an
  approved amount and fee. The fee cannot exceed the quoted percentage of the
  approved charge. The claimed provider cannot name itself as verifier.
  This is a **data contract**, not authentication: the caller is responsible for
  an independent, authorized verifier and useful-work reconciliation.
- `finalize` requires all claims resolved. It atomically moves the approved
  amount from the hold to provider-pending and fee accounts, and releases the
  remainder to the buyer. Provider-pending units have no cash-out path.
- An explicit `TestEarningRelease` can move one approved, settled, quoted
  receipt's net amount from provider-pending to a separate balance eligible
  only for **test access**. Its reviewer ID must differ from the provider ID;
  caller authentication and real independent review remain integration gates.
  Exact release replay is idempotent, and changed terms are rejected. A provider
  can atomically convert released test units into its own buyer wallet, where
  they can fund a bound test quote. Conversion IDs are replay safe and SQLite
  serialization prevents concurrent overspend. Neither method funds credits,
  enables resale, or creates a withdrawable balance. The independent SQLite
  prototype ran first; the focused earn-to-use script then passed in under one
  second, including reopen, v2 migration, concurrency and tamper checks.
- Every movement has equal debit and credit postings. `audit` checks event
  balance, materialized balances, held requests, quote binding, receipt caps,
  earning-release bindings, provider-pending totals and test conversion totals.
  Its read transaction makes that check one SQLite snapshot across writers.
  Schema-v1 files retain their existing unquoted holds as `legacy_unquoted`:
  new claims/approvals on those holds fail, while rejected or already-approved
  claims can be finalized and unused units returned. SQLite WAL
  with FULL synchronous mode supplies local process-crash persistence; separate
  backups, disk-loss recovery, fencing and replication are not proven.
- `buyer_wallet` provides one consistent read snapshot of noncash available
  units, active holds, pending claims, approved work awaiting settlement,
  settled spend and recent buyer postings. Its event IDs and deltas contain no
  conversation content or provider identity. SQLite insertion order is only a
  local display order; the view exposes no durable pagination cursor or live
  authentication boundary. The independent SQL prototype ran before this
  method was added; focused local checks cover the result, limits and reopen.
- `provider_wallet` reads a consistent, content-free view of submitted claims,
  approved work awaiting settlement, rejected claims and settled units still
  held in `provider_pending`, plus released test access units. It exposes no
  payout or cash eligibility.
  A focused standalone script first exercised its expected lifecycle and
  failed while the method was absent; it then passed in under one second after
  implementation, including reopen and invalid provider/limit checks. This is
  not a provider-authenticated wallet endpoint.

## Next integration boundary

1. Issue and authenticate the bound quote at API entry, link it to immutable
   request/attempt and route authority, and refuse unquoted admission. Shadow
   accounting must observe the request without changing access until B5
   evidence passes.
2. Implement versioned receipt producers for both whole-model and partial-stage
   work, with prefill/decode/cache/retry/failure/cancellation attribution and
   content-free evidence. Provider-reported token counts alone cannot approve a
   charge.
3. Provide an authenticated, independently operated validation interface and
   reconcile actual work to claims. Test forged useful work, model substitution,
   duplicate attempts, provider/client collusion, Sybil/circular activity and
   partition recovery. Respect private and Protected verification boundaries.
4. Add durable journal restore and daily reconciliation in the intended
   deployment topology. Define dispute/refund liability before any non-test
   funding or seller earnings can exist. B6 processor, payout and credit-resale
   gates remain separate and disabled.

The current module is not wired into `drift api` or the desktop. It is a
reviewable B5 accounting primitive only; the full beta remains open.
