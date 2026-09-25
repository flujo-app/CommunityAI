"""Standalone offline buy/earn/use/withdraw state-machine check in seconds."""

import importlib.util
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "src" / "drift" / "commerce_simulator.py"
spec = importlib.util.spec_from_file_location("commerce_simulator", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
CommerceSimulator = module.CommerceSimulator
CommerceSimulationError = module.CommerceSimulationError
SimulatedServiceQuote = module.SimulatedServiceQuote


def quote(request_id, buyer_id, cap, source="purchased", provider_id="provider_a"):
    return SimulatedServiceQuote(
        request_id=request_id, buyer_id=buyer_id, provider_id=provider_id, funding_source=source,
        model_id="test/model", profile_id="test/profile",
        service_class="text_inference", settlement_domain="local_simulation",
        artifact_sha256="a" * 64, service_policy_sha256="b" * 64,
        price_schedule_sha256="c" * 64,
        input_unit_price=1, output_unit_price=1,
        max_input_units=cap // 2, max_output_units=cap - cap // 2,
        fee_bps=2000, spend_cap=cap, expires_at_unix=int(time.time()) + 60,
    )


def denied(call):
    try:
        call()
    except CommerceSimulationError:
        return
    raise AssertionError("unsafe commerce transition accepted")


def check():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "commerce.db"
        with CommerceSimulator(path) as ledger:
            # Out-of-order verified callbacks cannot mint access before an order.
            assert ledger.record_verified_processor_event("rev_early", "processor_early", "reversal", 100)
            assert ledger.record_verified_processor_event("cap_early", "processor_early", "capture", 100)
            assert ledger.buyer_wallet("alice")["purchased_available"] == 0
            assert ledger.create_order("order_early", "alice", "processor_early", 100)
            assert ledger.buyer_wallet("alice")["purchased_available"] == 0
            assert ledger.record_verified_processor_event("cap_early_2", "processor_early", "capture", 100)
            assert ledger.buyer_wallet("alice")["purchased_available"] == 0

            assert ledger.create_order("order_main", "alice", "processor_main", 100)
            assert ledger.record_verified_processor_event("cap_main", "processor_main", "capture", 100)
            assert not ledger.record_verified_processor_event("cap_main", "processor_main", "capture", 100)
            assert ledger.record_verified_processor_event("cap_main_duplicate", "processor_main", "capture", 100)
            assert ledger.buyer_wallet("alice")["purchased_available"] == 100
            denied(lambda: ledger.record_verified_processor_event("cap_main", "processor_main", "capture", 99))
            denied(lambda: ledger.create_order("order_main", "alice", "processor_main", 101))

            main_quote = quote("request_main", "alice", 100)
            assert ledger.reserve_service(main_quote)
            assert not ledger.reserve_service(main_quote)
            denied(lambda: ledger.reserve_service(replace(main_quote, profile_id="test/changed")))
            assert ledger.buyer_wallet("alice")["service_held"] == 100
            denied(lambda: ledger.reserve_service(quote("request_more", "alice", 2)))
            denied(lambda: ledger.settle_service("request_main", "provider_a", 60, 13,
                                                  "decision_bad_fee", "a" * 64, 40, 20))
            denied(lambda: ledger.settle_service("request_main", "provider_other", 60, 10,
                                                  "decision_bad_provider", "a" * 64, 40, 20))
            denied(lambda: ledger.settle_service("request_main", "provider_a", 60, 10,
                                                  "decision_bad_units", "a" * 64, 51, 9))
            assert ledger.settle_service("request_main", "provider_a", 60, 10,
                                         "decision_main", "a" * 64, 40, 20)
            assert not ledger.settle_service("request_main", "provider_a", 60, 10,
                                             "decision_main", "a" * 64, 40, 20)
            denied(lambda: ledger.settle_service("request_main", "provider_a", 61, 10,
                                                  "decision_main", "a" * 64, 40, 20))
            assert ledger.buyer_wallet("alice")["purchased_available"] == 40
            assert ledger.provider_wallet("provider_a")["pending"] == 50
            assert ledger.release_earnings("release_main", "provider_a", 50, "risk_main")
            assert ledger.convert_earnings("convert_main", "provider_a", 20)
            assert ledger.buyer_wallet("provider_a")["earned_access_available"] == 20
            assert ledger.provider_wallet("provider_a")["eligible"] == 30
            assert ledger.reserve_service(quote("earned_request", "provider_a", 20, "earned", "provider_b"))
            assert ledger.refund_service("earned_request", "no_output")
            assert ledger.buyer_wallet("provider_a")["earned_access_available"] == 20

            assert ledger.request_payout("payout_main", "provider_a", 30, "external_main")
            assert ledger.mark_payout_unknown("payout_main")
            assert ledger.unresolved_payouts() == (("payout_main", "unknown"),)
            denied(lambda: ledger.request_payout("payout_again", "provider_a", 30, "external_again"))
            denied(lambda: ledger.convert_earnings("convert_again", "provider_a", 30))
            assert ledger.provider_wallet("provider_a")["payout_held"] == 30
            assert ledger.create_order("order_race", "buyer_race", "processor_race", 10)
            assert ledger.record_verified_processor_event("cap_race", "processor_race", "capture", 10)

            def race(request_id):
                try:
                    ledger.reserve_service(quote(request_id, "buyer_race", 10))
                    return request_id
                except CommerceSimulationError:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(race, ("race_a", "race_b")))
            assert sum(result is not None for result in results) == 1
            assert ledger.refund_service(next(result for result in results if result), "race_fixture")
            assert ledger.audit()["unfunded_reversal_loss"] == 0

        with CommerceSimulator(path) as reopened:
            assert reopened.unresolved_payouts() == (("payout_main", "unknown"),)
            assert reopened.resolve_payout("payout_main", "result_main", paid=True)
            assert not reopened.resolve_payout("payout_main", "result_main", paid=True)
            denied(lambda: reopened.resolve_payout("payout_main", "result_other", paid=True))
            denied(lambda: reopened.mark_payout_unknown("payout_main"))
            assert reopened.provider_wallet("provider_a")["payout_held"] == 0

            # Another verified purchase and payout failure returns the same held amount.
            assert reopened.create_order("order_second", "bob", "processor_second", 50)
            assert reopened.record_verified_processor_event("cap_second", "processor_second", "capture", 50)
            assert reopened.reserve_service(quote("request_second", "bob", 50, provider_id="provider_b"))
            assert reopened.settle_service("request_second", "provider_b", 50, 0,
                                           "decision_second", "b" * 64, 25, 25)
            assert reopened.release_earnings("release_second", "provider_b", 50, "risk_second")
            assert reopened.request_payout("payout_failed", "provider_b", 20, "external_failed")
            assert reopened.mark_payout_unknown("payout_failed")
            assert reopened.resolve_payout("payout_failed", "result_failed", paid=False)
            assert reopened.provider_wallet("provider_b")["eligible"] == 50

            # Late chargeback after service/payout records an explicit deficit.
            assert reopened.record_verified_processor_event("rev_main", "processor_main", "reversal", 100)
            assert reopened.buyer_wallet("alice")["purchased_available"] == 0
            assert reopened.audit()["unfunded_reversal_loss"] == 60
            denied(lambda: reopened.request_payout("payout_blocked", "provider_b", 20, "external_blocked"))
            denied(lambda: reopened.convert_earnings("conversion_blocked", "provider_b", 20))
            denied(lambda: reopened.release_earnings("release_blocked", "provider_b", 1, "risk_blocked"))
            denied(lambda: reopened.reserve_service(quote("admission_blocked", "buyer_race", 10)))

        # A changed quote digest must be caught by the audit after restart.
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("UPDATE quotes SET terms_digest=? WHERE request_id='request_main'", ("0" * 64,))
        with CommerceSimulator(path) as tampered:
            denied(tampered.audit)

    print("commerce simulator: reordered funding, holds, earnings, unknown payout, reversal loss PASS")


if __name__ == "__main__":
    check()
