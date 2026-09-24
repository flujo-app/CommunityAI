"""Focused, dependency-free checks for the shadow ledger.

Run with ``python scripts/check_shadow_credits.py``; this loads only the module
under test and Python's standard library, avoiding the project's ML stack.
"""

import importlib.util
import sys
import tempfile
import threading
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "drift" / "shadow_credits.py"
SPEC = importlib.util.spec_from_file_location("shadow_credits_standalone", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def rejected(call):
    try:
        call()
    except MODULE.ShadowCreditError:
        return
    raise AssertionError("invalid accounting operation accepted")


def check_settlement(path):
    receipt = MODULE.WorkReceipt("r1", "q1", "worker1", "whole", "a1", 10, 20, 60, "a" * 64)
    with MODULE.ShadowLedger(path) as ledger:
        assert ledger.grant_test_credits("g1", "alice", 100)
        assert not ledger.grant_test_credits("g1", "alice", 100)
        rejected(lambda: ledger.grant_test_credits("g1", "alice", 101))
        assert ledger.reserve("q1", "alice", 80)
        assert not ledger.reserve("q1", "alice", 80)
        rejected(lambda: ledger.reserve("q2", "alice", 21))
        assert ledger.submit_receipt(receipt)
        provider_before = ledger.provider_wallet("worker1")
        assert (provider_before["pending_review_receipts"], provider_before["approved_unsettled"]) == (1, 0)
        assert provider_before["settled_pending_balance"] == 0
        assert not ledger.submit_receipt(receipt)
        rejected(
            lambda: ledger.submit_receipt(
                MODULE.WorkReceipt("r1", "q1", "worker1", "whole", "a1", 10, 20, 61, "a" * 64)
            )
        )
        rejected(lambda: ledger.finalize("q1"))
        duplicate_attempt = MODULE.WorkReceipt("r2", "q1", "worker1", "whole", "a1", 10, 20, 60, "b" * 64)
        rejected(lambda: ledger.submit_receipt(duplicate_attempt))
        rejected(lambda: ledger.approve_receipt(MODULE.ValidationDecision("d0", "r1", "b" * 64, "verifier1", 50, 5)))
        rejected(
            lambda: ledger.approve_receipt(MODULE.ValidationDecision("d0", "r1", receipt.digest, "worker1", 50, 5))
        )
        decision = MODULE.ValidationDecision("d1", "r1", receipt.digest, "verifier1", 50, 5)
        assert ledger.approve_receipt(decision)
        assert not ledger.approve_receipt(decision)
        provider_approved = ledger.provider_wallet("worker1")
        assert (provider_approved["pending_review_receipts"], provider_approved["approved_unsettled"]) == (0, 50)
        held_wallet = ledger.buyer_wallet("alice")
        assert (held_wallet["available"], held_wallet["held"], held_wallet["approved_unsettled"]) == (20, 80, 50)
        assert ledger.finalize("q1")
        assert not ledger.finalize("q1")
        assert ledger.balance("buyer:alice") == 50
        assert ledger.balance("provider_pending:worker1") == 45
        provider_settled = ledger.provider_wallet("worker1")
        assert provider_settled["unit"] == "test_credit"
        assert provider_settled["settled_pending_balance"] == 45
        assert provider_settled["approved_unsettled"] == 0
        assert provider_settled["recent_events"][0]["pending_delta"] == 45
        assert len(ledger.provider_wallet("worker1", recent_limit=1)["recent_events"]) == 1
        rejected(lambda: ledger.provider_wallet("worker1", recent_limit=0))
        rejected(lambda: ledger.provider_wallet("unknown"))
        assert ledger.balance("fees") == 5
        assert ledger.balance("hold:q1") == 0
        wallet = ledger.buyer_wallet("alice")
        assert (wallet["available"], wallet["held"], wallet["pending_receipts"]) == (50, 0, 0)
        assert (wallet["approved_unsettled"], wallet["settled_spend"]) == (0, 50)
        assert wallet["unit"] == "test_credit"
        assert [event["available_delta"] for event in wallet["recent_events"]] == [30, -80, 100]
        assert len(ledger.buyer_wallet("alice", recent_limit=1)["recent_events"]) == 1
        rejected(lambda: ledger.buyer_wallet("alice", recent_limit=0))
        rejected(lambda: ledger.buyer_wallet("nobody"))
        assert ledger.audit() == {"accounts": 5, "events": 3, "reservations": 1, "receipts": 1}
    with MODULE.ShadowLedger(path) as reopened:
        assert reopened.audit()["events"] == 3
        assert not reopened.finalize("q1")
        assert reopened.balance("buyer:alice") == 50
        assert reopened.buyer_wallet("alice")["settled_spend"] == 50
        assert reopened.provider_wallet("worker1")["settled_pending_balance"] == 45


def check_cancellation_and_concurrency(path):
    with MODULE.ShadowLedger(path) as ledger:
        ledger.grant_test_credits("g2", "bob", 100)
        ledger.reserve("cancelled", "bob", 10)
        claim = MODULE.WorkReceipt("cancelled-receipt", "cancelled", "worker2", "whole", "attempt", 1, 0, 10, "c" * 64)
        ledger.submit_receipt(claim)
        assert ledger.buyer_wallet("bob")["pending_receipts"] == 1
        assert all("q1" not in event["event_id"] for event in ledger.buyer_wallet("bob")["recent_events"])
        ledger.reject_receipt(claim.receipt_id, "no_useful_work")
        assert ledger.provider_wallet("worker2")["rejected_receipts"] == 1
        assert not ledger.reject_receipt(claim.receipt_id, "no_useful_work")
        ledger.finalize("cancelled")
        assert ledger.balance("buyer:bob") == 100
        assert ledger.buyer_wallet("bob")["held"] == 0
        assert ledger.audit()["receipts"] == 2

    barrier = threading.Barrier(2)
    outcomes = []

    def contender(request_id):
        with MODULE.ShadowLedger(path) as candidate:
            barrier.wait()
            try:
                candidate.reserve(request_id, "bob", 80)
                outcomes.append("held")
            except MODULE.ShadowCreditError:
                outcomes.append("refused")

    threads = [threading.Thread(target=contender, args=(f"race{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["held", "refused"]
    with MODULE.ShadowLedger(path) as ledger:
        assert ledger.balance("buyer:bob") == 20
        assert ledger.audit()["reservations"] == 3


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shadow.sqlite3"
        check_settlement(path)
        check_cancellation_and_concurrency(path)
    print("shadow-credit checks: receipt replay, cap, settlement, cancellation, restart and concurrent holds PASS")


if __name__ == "__main__":
    main()
