"""Focused noncash earn-to-use checks, without the project's ML dependencies."""

import sqlite3
import tempfile
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from check_shadow_credits import MODULE, quote, rejected


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "earnings.sqlite"
        receipt = MODULE.WorkReceipt("work-1", "request-1", "provider-1", "whole", "attempt-1", 50, 0, 50, "d" * 64)
        release = MODULE.TestEarningRelease("release-1", receipt.receipt_id, receipt.digest, "provider-1", "reviewer-1")
        with MODULE.ShadowLedger(path) as ledger:
            ledger.grant_test_credits("grant-1", "buyer-1", 100)
            ledger.reserve(quote("request-1", "buyer-1", 60))
            ledger.submit_receipt(receipt)
            rejected(lambda: ledger.release_test_earning(release))
            decision = MODULE.ValidationDecision("decision-1", receipt.receipt_id, receipt.digest, "verifier-1", 50, 5)
            ledger.approve_receipt(decision)
            rejected(lambda: ledger.release_test_earning(release))
            ledger.finalize("request-1")
            rejected(
                lambda: MODULE.TestEarningRelease("bad", receipt.receipt_id, receipt.digest, "provider-1", "provider-1")
            )
            rejected(lambda: ledger.release_test_earning(replace(release, receipt_digest="e" * 64)))
            rejected(lambda: ledger.release_test_earning(replace(release, provider_id="provider-2")))
            assert ledger.release_test_earning(release)
            assert not ledger.release_test_earning(release)
            rejected(lambda: ledger.release_test_earning(replace(release, reviewer_id="reviewer-2")))
            assert ledger.provider_wallet("provider-1")["eligible_test_access_balance"] == 45
            assert ledger.provider_wallet("provider-1")["settled_pending_balance"] == 0
            assert ledger.convert_test_earnings("convert-1", "provider-1", 20)
            assert not ledger.convert_test_earnings("convert-1", "provider-1", 20)
            rejected(lambda: ledger.convert_test_earnings("convert-1", "provider-1", 21))
            rejected(lambda: ledger.convert_test_earnings("overspend", "provider-1", 26))
            rejected(lambda: ledger.convert_test_earnings("wrong-provider", "provider-2", 1))
            assert ledger.buyer_wallet("provider-1")["available"] == 20
            assert ledger.audit()["earning_releases"] == 1
        outcomes = []
        barrier = threading.Barrier(2)

        def contender(identifier):
            with MODULE.ShadowLedger(path) as ledger:
                barrier.wait()
                try:
                    outcomes.append(ledger.convert_test_earnings(identifier, "provider-1", 20))
                except MODULE.ShadowCreditError:
                    outcomes.append(False)

        threads = [threading.Thread(target=contender, args=(f"race-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert sorted(outcomes) == [False, True]
        with MODULE.ShadowLedger(path) as ledger:
            assert ledger.provider_wallet("provider-1")["eligible_test_access_balance"] == 5
            assert ledger.buyer_wallet("provider-1")["available"] == 40
            assert ledger.reserve(quote("provider-request", "provider-1", 10))
            assert ledger.buyer_wallet("provider-1")["available"] == 30
            assert ledger.audit()["earning_releases"] == 1
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=2")
            db.commit()
        with MODULE.ShadowLedger(path) as migrated:
            assert migrated.audit()["earning_releases"] == 1
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE earning_releases SET net_units=44 WHERE receipt_id='work-1'")
            db.commit()
        with MODULE.ShadowLedger(path) as tampered:
            rejected(tampered.audit)
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE earning_releases SET net_units=45 WHERE receipt_id='work-1'")
            db.execute("UPDATE events SET payload_digest=? WHERE event_id='convert:convert-1'", ("0" * 64,))
            db.commit()
        with MODULE.ShadowLedger(path) as tampered:
            rejected(tampered.audit)
    print("test earnings conversion PASS")


if __name__ == "__main__":
    main()
