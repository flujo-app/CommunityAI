# Payment and payout provider preflight

Status: source review on 2026-09-24, **no account approval or live payment
capability verified**. This is an implementation input for B6, not a processor
selection, legal opinion, or permission to transact.

The owner operates as an individual in Colombia and reports existing PayPal
Business and Payoneer accounts. Those facts do not establish that either
account can run a compute marketplace, receive buyer funds for future service,
or pay providers. The signed-in account session was not available to this task's
read-only browser inventory, so no account feature or balance was inspected.

## Product constraints from current provider documentation

- PayPal's [Complete Payments Platform start guide](https://developer.paypal.com/platforms/get-started/)
  says marketplace/platform APIs require partner approval; unapproved live API
  calls return 401. Sandbox calls can precede approval. A PayPal Business login
  alone is therefore insufficient evidence of live platform capability.
- PayPal's [seller onboarding guide](https://developer.paypal.com/platforms/seller-onboarding)
  lists Colombia for specific **business-seller** onboarding flows, with other
  columns unavailable. Seller account type, product, timing and platform intent
  affect eligibility; that table does not approve CommunityAI's operator account
  or any individual provider.
- PayPal's [Payouts feature matrix](https://developer.paypal.com/payouts/supported-features/)
  gives Colombia a cross-border-only restriction for COP payouts. Payout
  direction, currency, recipient country and actual account enablement must be
  checked before choosing a launch corridor.
- Payoneer's [Mass Payout API guide](https://www.payoneer.com/developers-docs/mass-payout/mass-submit-payout/)
  requires registered payees and prefunded payouts. Submission is asynchronous,
  so a successful request cannot itself settle a provider payable. Its
  [marketplace page](https://www.payoneer.com/marketplace/) describes API and
  seller onboarding products but does not establish access for this account.

## Engineering disposition

Keep the ledger's current test-unit grants and earn-to-use conversion strictly
noncash. Build a provider-neutral simulated funding and payout state machine
with external event IDs, durable idempotency, pending/unknown states, reversal
and reconciliation before binding any account. Store purchased access,
promotions and payable earnings in separate provenances; a test-unit grant must
never become withdrawable earnings. Do not infer payout finality from a webhook
or API submission alone.

For a live adapter, first verify the actual operator product approval,
country/currency corridor, buyer funding and refund terms, payee onboarding,
available reserves and payout controls through the account's secure interface.
The $0 new-spending constraint and absent funded risk reserve keep live buyer
funding, provider payouts and literal credit resale disabled. A sandbox run may
validate the integration protocol but cannot close these activation gates.
