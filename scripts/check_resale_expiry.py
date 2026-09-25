"""Subsecond noncash expiry and ordered-listing recovery check."""

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from check_credit_resale_simulator import CommerceSimulator, CommerceSimulationError, quote


def denied(action):
    try:
        action()
    except CommerceSimulationError:
        return
    raise AssertionError("invalid expiry limit accepted")


def main():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "credits.db"
        with CommerceSimulator(path) as ledger:
            ledger.create_order("seed_order", "buyer", "seed_ref", 40)
            ledger.record_verified_processor_event("seed_cap", "seed_ref", "capture", 40)
            ledger.reserve_service(quote("seed_work", "buyer", 40, "purchased", "seller"))
            ledger.settle_service("seed_work", "seller", 40, 0, "seed_decision", "a" * 64, 20, 20)
            ledger.release_earnings("seed_release", "seller", 40, "seed_risk")
            ledger.convert_earnings("seed_convert", "seller", 40)
            now = int(time.time())
            for lot in ("old_a", "old_b", "ordered"):
                ledger.list_earned_credits(lot, "seller", 10, 10, 0, now + 30)
            ledger.list_earned_credits("future", "seller", 10, 10, 0, now + 120)
            ledger.place_resale_order("ordered", "buyer_two", "ordered_ref")
            assert ledger.buyer_wallet("seller")["earned_access_available"] == 0
            assert ledger.expire_open_resale_listings() == ()
            for invalid in (0, -1, 1001, True, 1.5):
                denied(lambda invalid=invalid: ledger.expire_open_resale_listings(limit=invalid))

            with patch("time.time", return_value=now + 60):
                assert ledger.expire_open_resale_listings(limit=1) == ("old_a",)
                assert ledger.expire_open_resale_listings(limit=1) == ("old_b",)
                assert ledger.expire_open_resale_listings(limit=1) == ()
            assert ledger.buyer_wallet("seller")["earned_access_available"] == 20
            assert dict(ledger._db.execute(
                "SELECT listing_id,status FROM resale_listings"
            )) == {"old_a": "cancelled", "old_b": "cancelled",
                   "ordered": "ordered", "future": "open"}
            ledger.audit()

        with CommerceSimulator(path) as reopened:
            with patch("time.time", return_value=now + 60):
                assert reopened.expire_open_resale_listings() == ()
            # A payment outcome can still resolve a listing ordered before expiry.
            reopened.record_verified_resale_event("ordered_rev", "ordered_ref", "reversal", 10)
            reopened.record_verified_resale_event("ordered_cap", "ordered_ref", "capture", 10)
            assert reopened.buyer_wallet("seller")["earned_access_available"] == 30
            assert reopened._db.execute(
                "SELECT status FROM resale_listings WHERE listing_id='ordered'"
            ).fetchone() == ("reversed",)
            reopened.audit()
    print("PASS: bounded expiry reclamation, replay, and ordered payment resolution")


if __name__ == "__main__":
    main()
