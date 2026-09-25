"""Standalone noncash commerce-state experiment; no provider or API calls."""


def apply_events(order_amount: int, observed: tuple[tuple[str, int], ...]) -> tuple[int, str]:
    if type(order_amount) is not int or order_amount <= 0:
        raise ValueError("invalid order")
    kinds = {}
    for kind, amount in observed:
        if kind not in {"capture", "reversal"} or amount != order_amount:
            raise ValueError("invalid verified event")
        kinds[kind] = amount
    if "capture" not in kinds:
        return 0, "pending"
    if "reversal" in kinds:
        return 0, "reversed"
    return order_amount, "captured"


class Payout:
    def __init__(self, eligible: int, amount: int):
        if amount <= 0 or eligible < amount:
            raise ValueError("insufficient eligible earnings")
        self.eligible = eligible - amount
        self.held = amount
        self.state = "reserved"

    def unknown(self):
        if self.state not in {"reserved", "unknown"}:
            raise ValueError("terminal payout")
        self.state = "unknown"

    def resolve(self, paid: bool):
        if self.state not in {"reserved", "unknown"}:
            raise ValueError("terminal payout")
        if not paid:
            self.eligible += self.held
        self.held = 0
        self.state = "paid" if paid else "failed"


if __name__ == "__main__":
    assert apply_events(100, (("reversal", 100),)) == (0, "pending")
    assert apply_events(100, (("reversal", 100), ("capture", 100))) == (0, "reversed")
    assert apply_events(100, (("capture", 100),)) == (100, "captured")
    assert apply_events(100, (("capture", 100), ("capture", 100))) == (100, "captured")
    payout = Payout(80, 30)
    payout.unknown()
    assert (payout.eligible, payout.held, payout.state) == (50, 30, "unknown")
    payout.resolve(True)
    assert (payout.eligible, payout.held, payout.state) == (50, 0, "paid")
    try:
        payout.resolve(True)
    except ValueError:
        pass
    else:
        raise AssertionError("payout retried after terminal result")
    print("noncash commerce lifecycle prototype PASS")
