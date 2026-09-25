"""Tiny noncash posting experiment for earned-credit resale and late reversal."""

from collections import defaultdict


def main() -> None:
    balance = defaultdict(int)

    def post(*entries):
        assert sum(delta for _, delta in entries) == 0
        for account, delta in entries:
            balance[account] += delta
        assert all(value >= 0 for account, value in balance.items() if account not in {"external", "loss"})
        assert sum(balance.values()) == 0

    # Prior independently qualified work had already converted 20 eligible
    # earnings into seller-owned access. That source remains separately funded.
    post(("external", -20), ("seller_earned", 20))
    post(("seller_earned", -20), ("listing_hold", 20))
    assert balance["seller_earned"] == 0 and balance["listing_hold"] == 20

    # Verified buyer capture transfers the existing claim and holds seller cash.
    post(("listing_hold", -20), ("buyer_resale", 20))
    post(("external", -20), ("seller_payable", 18), ("fees", 2))
    assert balance["buyer_resale"] == 20 and balance["seller_payable"] == 18

    # Buyer spends five credits; seller receives a separate risk release and
    # ten units of payout before a late payment reversal arrives.
    post(("buyer_resale", -5), ("service_cost", 5))
    post(("seller_payable", -18), ("seller_eligible", 18))
    post(("seller_eligible", -10), ("external", 10))

    buyer_recovered = min(balance["buyer_resale"], 20)
    credit_loss = 20 - buyer_recovered
    post(("buyer_resale", -buyer_recovered), ("loss", -credit_loss), ("seller_earned", 20))
    seller_recovered = min(balance["seller_eligible"], 18)
    cash_loss = 18 - seller_recovered
    post(("seller_eligible", -seller_recovered), ("fees", -2), ("loss", -cash_loss), ("external", 20))
    assert balance["seller_earned"] == 20 and balance["buyer_resale"] == 0
    assert balance["loss"] == -15 and balance["seller_eligible"] == 0
    print("PASS: resale transfer and reversal expose the spent-credit and paid-seller deficits")


if __name__ == "__main__":
    main()
