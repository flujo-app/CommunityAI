"""Standalone SQL prototype for a buyer's noncash shadow-wallet view."""

import importlib.util
import sys
import tempfile
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "src" / "drift" / "shadow_credits.py"
spec = importlib.util.spec_from_file_location("shadow_credits_wallet_prototype", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def quote(request_id, cap, input_price, output_price, max_input, max_output):
    return module.ShadowQuote(
        request_id,
        "alice",
        "test/model",
        "test/profile",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "standard",
        "test",
        input_price,
        output_price,
        max_input,
        max_output,
        1000,
        cap,
        4_102_444_800,
    )


def main():
    with tempfile.TemporaryDirectory() as directory:
        with module.ShadowLedger(Path(directory) / "wallet.sqlite3") as ledger:
            ledger.grant_test_credits("g1", "alice", 100)
            ledger.reserve(quote("q1", 70, 4, 4, 5, 5))
            receipt = module.WorkReceipt("r1", "q1", "worker", "whole", "a1", 5, 5, 40, "a" * 64)
            ledger.submit_receipt(receipt)
            ledger.approve_receipt(module.ValidationDecision("d1", "r1", receipt.digest, "verifier", 30, 3))
            ledger.finalize("q1")
            ledger.reserve(quote("q2", 20, 2, 3, 5, 3))
            receipt2 = module.WorkReceipt("r2", "q2", "worker", "whole", "a2", 1, 1, 5, "b" * 64)
            ledger.submit_receipt(receipt2)
            db = ledger._db
            available = db.execute("SELECT balance FROM accounts WHERE account_id=?", ("buyer:alice",)).fetchone()[0]
            held = db.execute(
                "SELECT COALESCE(SUM(cap),0) FROM reservations WHERE buyer_id=? AND status='held'", ("alice",)
            ).fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) FROM receipts r JOIN reservations v USING(request_id) "
                "WHERE v.buyer_id=? AND r.status='pending'",
                ("alice",),
            ).fetchone()[0]
            spent = db.execute(
                "SELECT COALESCE(SUM(r.approved_charge),0) FROM receipts r "
                "JOIN reservations v USING(request_id) "
                "WHERE v.buyer_id=? AND v.status='settled' AND r.status='approved'",
                ("alice",),
            ).fetchone()[0]
            events = db.execute(
                "SELECT e.event_id,e.kind,p.delta FROM events e JOIN postings p USING(event_id) "
                "WHERE p.account_id=? ORDER BY e.rowid DESC LIMIT 10",
                ("buyer:alice",),
            ).fetchall()
            assert (available, held, pending, spent) == (50, 20, 1, 30)
            assert events == [
                ("reserve:q2", "reserve", -20),
                ("finalize:q1", "finalize", 40),
                ("reserve:q1", "reserve", -70),
                ("grant:g1", "test_grant", 100),
            ]
    print("shadow-wallet SQL prototype: snapshot and recent buyer postings PASS")


if __name__ == "__main__":
    main()
