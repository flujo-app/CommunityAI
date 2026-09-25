"""Focused, dependency-free checks for the shadow ledger.

Run with ``python scripts/check_shadow_credits.py``; this loads only the module
under test and Python's standard library, avoiding the project's ML stack.
"""

import importlib.util
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

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


def quote(
    request_id, buyer_id, cap, *, input_price=1, output_price=1, max_input=None, max_output=0, model="test/model"
):
    return MODULE.ShadowQuote(
        request_id,
        buyer_id,
        model,
        "test/profile",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "standard",
        "test",
        input_price,
        output_price,
        cap if max_input is None else max_input,
        max_output,
        1000,
        cap,
        4_102_444_800,
    )


def check_settlement(path):
    receipt = MODULE.WorkReceipt("r1", "q1", "worker1", "whole", "a1", 10, 20, 60, "a" * 64)
    with MODULE.ShadowLedger(path) as ledger:
        assert ledger.grant_test_credits("g1", "alice", 100)
        assert not ledger.grant_test_credits("g1", "alice", 100)
        rejected(lambda: ledger.grant_test_credits("g1", "alice", 101))
        service = quote("q1", "alice", 80, input_price=2, output_price=2, max_input=10, max_output=30)
        assert ledger.reserve(service)
        assert not ledger.reserve(service)
        with patch.object(MODULE.time, "time", return_value=4_102_444_801):
            assert not ledger.reserve(service)
        rejected(
            lambda: ledger.reserve(
                quote(
                    "q1", "alice", 80, input_price=2, output_price=2, max_input=10, max_output=30, model="other/model"
                )
            )
        )
        rejected(lambda: ledger.reserve(quote("q2", "alice", 21)))
        rejected(lambda: ledger.reserve(replace(quote("expired", "alice", 1), expires_at_unix=1)))
        rejected(lambda: quote("overpriced", "alice", 1, input_price=2, max_input=1))
        rejected(lambda: replace(service, fee_bps=10_001))
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
        rejected(
            lambda: ledger.submit_receipt(
                MODULE.WorkReceipt("too-much", "q1", "worker1", "whole", "a2", 11, 0, 22, "b" * 64)
            )
        )
        rejected(
            lambda: ledger.submit_receipt(
                MODULE.WorkReceipt("too-pricey", "q1", "worker1", "whole", "a3", 1, 1, 5, "b" * 64)
            )
        )
        rejected(
            lambda: ledger.submit_receipt(
                MODULE.WorkReceipt("aggregate-input", "q1", "worker1", "whole", "a4", 1, 0, 2, "b" * 64)
            )
        )
        rejected(
            lambda: ledger.submit_receipt(
                MODULE.WorkReceipt("aggregate-output", "q1", "worker1", "whole", "a5", 0, 11, 22, "b" * 64)
            )
        )
        duplicate_attempt = MODULE.WorkReceipt("r2", "q1", "worker1", "whole", "a1", 10, 20, 60, "b" * 64)
        rejected(lambda: ledger.submit_receipt(duplicate_attempt))
        rejected(lambda: ledger.approve_receipt(MODULE.ValidationDecision("d0", "r1", "b" * 64, "verifier1", 50, 5)))
        rejected(
            lambda: ledger.approve_receipt(MODULE.ValidationDecision("d0", "r1", receipt.digest, "worker1", 50, 5))
        )
        rejected(
            lambda: ledger.approve_receipt(MODULE.ValidationDecision("d0", "r1", receipt.digest, "verifier1", 50, 6))
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
        assert ledger.audit() == {
            "accounts": 5,
            "events": 3,
            "reservations": 1,
            "receipts": 1,
            "quotes": 1,
            "legacy_unquoted": 0,
            "earning_releases": 0,
        }
    with MODULE.ShadowLedger(path) as reopened:
        assert reopened.audit()["events"] == 3
        assert not reopened.finalize("q1")
        assert reopened.balance("buyer:alice") == 50
        assert reopened.buyer_wallet("alice")["settled_spend"] == 50
        assert reopened.provider_wallet("worker1")["settled_pending_balance"] == 45


def check_cancellation_and_concurrency(path):
    with MODULE.ShadowLedger(path) as ledger:
        ledger.grant_test_credits("g2", "bob", 100)
        ledger.reserve(quote("cancelled", "bob", 10, input_price=10, max_input=1))
        claim = MODULE.WorkReceipt("cancelled-receipt", "cancelled", "worker2", "whole", "attempt", 1, 0, 10, "c" * 64)
        ledger.submit_receipt(claim)
        assert ledger.buyer_wallet("bob")["pending_receipts"] == 1
        assert all("q1" not in event["event_id"] for event in ledger.buyer_wallet("bob")["recent_events"])
        ledger.reject_receipt(claim.receipt_id, "no_useful_work")
        assert ledger.provider_wallet("worker2")["rejected_receipts"] == 1
        assert not ledger.reject_receipt(claim.receipt_id, "no_useful_work")
        retry = MODULE.WorkReceipt("cancelled-retry", "cancelled", "worker2", "whole", "retry", 1, 0, 10, "d" * 64)
        assert ledger.submit_receipt(retry)
        ledger.reject_receipt(retry.receipt_id, "cancelled")
        ledger.finalize("cancelled")
        assert ledger.balance("buyer:bob") == 100
        assert ledger.buyer_wallet("bob")["held"] == 0
        assert ledger.audit()["receipts"] == 3

    barrier = threading.Barrier(2)
    outcomes = []

    def contender(request_id):
        with MODULE.ShadowLedger(path) as candidate:
            barrier.wait()
            try:
                candidate.reserve(quote(request_id, "bob", 80))
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


def check_legacy_migration(path):
    old_hold = quote("legacy", "legacy_buyer", 10)
    old_receipt = MODULE.WorkReceipt("legacy_old", "legacy", "worker", "whole", "old", 1, 0, 1, "a" * 64)
    with MODULE.ShadowLedger(path) as ledger:
        ledger.grant_test_credits("legacy_grant", "legacy_buyer", 10)
        ledger.reserve(old_hold)
        ledger.submit_receipt(old_receipt)
    # Re-create the exact table-presence/version shape of a v1 file. Its hold
    # has no trusted quote after migration, regardless of prior event payload.
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute("DROP TABLE quotes")
            db.execute("DROP TABLE legacy_unquoted")
            db.execute("PRAGMA user_version=1")
    with MODULE.ShadowLedger(path) as migrated:
        assert migrated.audit()["legacy_unquoted"] == 1
        rejected(lambda: migrated.reserve(old_hold))
        rejected(
            lambda: migrated.submit_receipt(
                MODULE.WorkReceipt("legacy_r", "legacy", "worker", "whole", "attempt", 1, 0, 1, "a" * 64)
            )
        )
        rejected(
            lambda: migrated.approve_receipt(
                MODULE.ValidationDecision(
                    "legacy_decision", old_receipt.receipt_id, old_receipt.digest, "verifier", 1, 0
                )
            )
        )
        migrated.reject_receipt(old_receipt.receipt_id, "unquoted_legacy_claim")
        assert migrated.finalize("legacy")
        assert migrated.balance("buyer:legacy_buyer") == 10
        assert migrated.audit()["legacy_unquoted"] == 1


def check_quote_tamper(path):
    service = quote("tamper", "alice", 10)
    with MODULE.ShadowLedger(path) as ledger:
        ledger.grant_test_credits("tamper_grant", "alice", 10)
        ledger.reserve(service)
        changed = replace(service, model_id="other/model")
        ledger._db.execute(
            "UPDATE quotes SET terms_json=?, terms_digest=? WHERE request_id=?",
            (MODULE._canonical_json(asdict(changed)), changed.digest, service.request_id),
        )
        rejected(lambda: ledger.reserve(changed))
        rejected(lambda: ledger.audit())


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shadow.sqlite3"
        check_settlement(path)
        check_cancellation_and_concurrency(path)
        check_legacy_migration(Path(directory) / "legacy.sqlite3")
        check_quote_tamper(Path(directory) / "tamper.sqlite3")
    print("shadow-credit checks: bound quote, rates, fee, replay, settlement, migration and concurrent holds PASS")


if __name__ == "__main__":
    main()
