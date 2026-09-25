# B6c: literal credit resale disposition

Status: **proposed mechanism; disabled and unimplemented**. This is a concrete
design for the user's requested sale of credits between users. Selling compute,
converting provider earnings into access, and withdrawing earnings remain
different transactions. No provider approval, legal classification, reserve,
live buyer funding, seller payout or account capability has been verified.

## Proposed supported transaction

Only access credits converted from **eligible, independently validated provider
earnings** may be listed. The seller chooses a fixed credit quantity and price
in a single supported currency. An accepted listing reserves those credits so
the seller cannot spend or withdraw the underlying earnings. Purchased access,
promotional/test grants, pending work claims, disputed earnings and credits
previously acquired through resale are ineligible. A buyer cannot be the same
person, account, or related beneficiary as the seller. An expired or cancelled
listing releases its hold without changing either user's balance.

An immutable buyer quote binds the seller/listing, buyer, credit quantity,
currency, gross price, disclosed operator fee, seller net amount, expiry,
provenance, payment processor reference and risk limits. The buyer's payment
must be captured and independently reconciled before transfer. In one durable
journal transaction, consume the seller's credit hold, give the buyer a
**resale-origin, nontransferable** access balance, and create a pending seller
payable. This is a literal user-to-user transfer of an existing service claim;
it does not mint a second access claim when the payment notification arrives.
Buyer access may be spent only on qualified CommunityAI service. The seller
payable remains in a risk hold until eligibility and reversal exposure checks
release it; only then can it enter the existing payout reservation workflow.
Cash, service credits and provider payables require separate currencies and
accounts, with a fixed quote rate and explicit fee postings.

Duplicate, reordered and conflicting payment notifications must use the same
durable inbox and idempotency rules as ordinary purchases. A reversal before
transfer releases the seller hold and leaves the buyer unfunded. A reversal
after transfer first recovers unspent resale-origin access, then claws back
unreleased seller payable; any shortfall is an explicit operator loss that
freezes new resale, payouts and paid service until a funded reserve covers it.
Payout submission is not payout finality. Partial refunds, service failures,
chargebacks after payout, tax handling and customer support outcomes need
their own reviewed states before activation.

The first permitted implementation would be a capped, one-hop market with
enrolled adult sellers and buyers, one currency/corridor, fixed listings,
per-account and daily limits, related-account checks, a funded loss reserve,
manual dispute controls, and no open order book or transferable token. These
limits are proposed engineering terms, not a claim that a payment provider
accepts them. No UI or API should expose resale until the commercial gates
below are met.

## Provider and operating decision

The [PayPal Colombia Acceptable Use Policy](https://www.paypal.com/co/legalhub/paypal/acceptableuse-full?locale.x=en_CO)
lists pre-approval for payment-facilitator/stored-value services, digitally
tradable or transferable value, and marketplaces. The proposed transaction may
fall into one or more of those categories; only PayPal can confirm treatment
and approve this operator and product. PayPal's [platform start
guide](https://developer.paypal.com/platforms/get-started/) also requires
partner approval for live marketplace APIs. A Business login does not prove
either approval.

Payoneer's current [prohibited transaction
list](https://www.payoneer.com/legal/prohibited-transaction-list/) says service
depends on business, country, approvals and risk policy; it lists virtual
currencies, payment processing, unlicensed escrow/money services and personal
peer-to-peer payments among restricted categories. It says Payoneer may
pre-approve some listed activities after review. The exact treatment of these
closed-loop compute credits, and whether a specific operator account can
receive buyer funds and pay sellers, is unknown. Do not infer permission from
Payoneer's general marketplace marketing page or from account ownership.

The operator review package is now specific enough to seek written answers:

1. May the Colombia-based individual operator run **buyer-funded compute
   access and provider payouts** under the proposed merchant/platform role?
2. May enrolled providers resell *earned, closed-loop service credits* to
   another user for cash through this account/product? Does this require a
   separate stored-value, virtual-currency, marketplace or payment-facilitator
   approval, license, or entity?
3. Which buyer/seller countries, currencies, payee account types and payout
   corridors are enabled, and who bears refund, dispute and chargeback losses?
4. What seller onboarding, identity, transaction monitoring, tax/reporting,
   reserve, fee disclosure and customer-funds terms are required?
5. Which sandbox products and webhook/reconciliation APIs correspond to the
   approved live configuration? Can provider approval be documented in writing
   before credentials or funds are used?

No answer has been supplied yet. With the current $0 new-spending budget and
no funded loss reserve, the code must remain in simulated/test mode. If the
available legitimate providers reject the transaction, literal credit resale
remains an explicit unmet release requirement; earnings withdrawal cannot be
relabeled as its fulfillment. The owner can then choose a legitimately approved
provider/setup or explicitly change product scope. Neither choice is presumed.

## Implementation and evidence still required

- Extend the durable commerce journal with resale listings, seller credit
  holds, buyer funding events, one-hop transfer provenance, seller payable
  holds, reversals and reserve accounting. Test replay, restore, concurrent
  spend/list/convert/payout, related accounts and reversal-after-use/payout in
  a standalone script before wider CI.
- Bind a verified processor adapter and daily reconciliation to the actual
  approved account/corridor. Keep webhook authenticity, order ownership and
  capture/refund identity outside untrusted client requests.
- Confirm provider and jurisdiction treatment, seller eligibility, funded
  liability, support and reviewed customer terms. Then test sandbox behavior,
  installed UX, failure recovery and a bounded live transaction only after
  explicit account and funding evidence exists.

This document resolves the *proposed mechanism* part of B6c. It is not the B6c
activation gate or a claim that the full beta is commercially ready.
