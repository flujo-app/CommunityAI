# Noncash commerce lifecycle simulator

Status: **local simulated verification only**. `src/drift/commerce_simulator.py` has no payment credentials, provider calls, webhook endpoint or live-money interface. Its `record_verified_processor_event` and payout-resolution methods assume a future adapter has authenticated and reconciled the external event; the simulator itself cannot verify PayPal, Payoneer or a bank. No actual account capability or funding was observed.

The standalone in-memory experiment `scripts/prototype_commerce_lifecycle.py` ran first. The durable SQLite simulator then implements the following synthetic state transitions with balanced postings and replay checks:

| Flow | Simulated behavior |
| --- | --- |
| Buy | Purchase intent remains unfunded until a capture event. The durable inbox accepts reversal-before-capture and event-before-order, then applies capture and reversal atomically once both can be matched. Duplicate event IDs or multiple notifications for the same economic capture do not mint access. |
| Use | Immutable quote binds buyer, provider route, purchased/earned provenance, model/profile/artifact, price schedule, fee bound, token bounds, expiry and maximum spend. The full cap is held before simulated service; a trusted test decision can settle accepted units and release unused hold, or an unserved request can be refunded. |
| Earn | Settled provider payables stay pending until a separate risk release. Eligible earnings can be converted to a distinct earned-access balance. Conversion and payout reserve compete for the same eligible balance. |
| Sell compute / withdraw | Payout requests reserve eligible earnings. A timeout/unknown outcome keeps the hold through restart. Only an externally verified final success or failure would close it; failure restores eligible earnings, success removes the hold. |
| Reversal after use | A late reversal recovers what remains in the purchased wallet and records any shortfall as explicit operator loss. Unfunded loss blocks new service admission, earning release, conversion and payouts. It does not erase a completed external payout. |

The provider fee is included within the buyer's simulated service charge. Purchased access and converted earned access occupy different accounts; promotional/test grants from `shadow_credits.py` cannot enter this journal. Literal resale of credits has no transfer operation and remains an open B6c requirement. Current service decisions and risk releases are trusted **fixtures**, not independent useful-work validation or fraud controls. The simulator has no processor/bank reconciliation, funded loss reserve, partial refund or country/currency activation. It cannot make customer balances cash or seller earnings withdrawable.

The proposed earned-credit, one-hop resale transaction and provider questions
are recorded in [B6c credit resale disposition](CREDIT_RESALE_DISPOSITION_2026-09-25.md).
It remains disabled; this simulator has no resale listing or transfer state.

The standalone `scripts/check_commerce_simulator.py` passed in about 0.3 seconds on the local Python runtime. It exercises reordered and duplicate funding events; quote conflicts, rate/fee bounds and concurrent double-reserve; buy → use → earn → convert and payout; payout uncertainty across restart; payout failure and replay; a late reversal deficit and admission freeze; and an audit-detected quote tamper. No broad CI or external transaction was run.

[PayPal's webhook guidance](https://developer.paypal.com/api/rest/webhooks/) requires message verification and describes redelivery after non-2xx responses. [Payoneer's Mass Payout guidance](https://www.payoneer.com/developers-docs/mass-payout/mass-submit-payout/) describes prefunded, registered-payee, asynchronous payouts. A live adapter must satisfy those provider-specific contracts, verify the owner's actual account eligibility and process real events durably before acknowledgement. The owner-provided $0 new-spending limit and missing funded loss owner keep activation disabled.
