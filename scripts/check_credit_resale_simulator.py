"""Standalone noncash B6c journal check; no payment account or network."""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "src" / "drift" / "commerce_simulator.py"
spec = importlib.util.spec_from_file_location("commerce_simulator_resale", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
CommerceSimulator = module.CommerceSimulator
CommerceSimulationError = module.CommerceSimulationError
SimulatedServiceQuote = module.SimulatedServiceQuote


def quote(request_id, buyer_id, cap, source, provider_id, resale_listing_id=None):
    return SimulatedServiceQuote(
        request_id=request_id, buyer_id=buyer_id, provider_id=provider_id, funding_source=source,
        model_id="test/model", profile_id="test/profile", service_class="text_inference",
        settlement_domain="local_simulation", artifact_sha256="a" * 64,
        service_policy_sha256="b" * 64, price_schedule_sha256="c" * 64,
        input_unit_price=1, output_unit_price=1, max_input_units=cap // 2,
        max_output_units=cap - cap // 2, fee_bps=0, spend_cap=cap,
        expires_at_unix=int(time.time()) + 60,
        version=2 if source == "resale" else 1, resale_listing_id=resale_listing_id,
    )


def denied(action):
    try:
        action()
    except CommerceSimulationError:
        return
    raise AssertionError("unsafe resale transition accepted")


def check_resale(path):
    expiry = int(time.time()) + 60
    with CommerceSimulator(path) as ledger:
        # Independently accepted useful-work fixture creates eligible earnings.
        assert ledger.create_order("seed_order", "seed_buyer", "seed_ref", 100)
        assert ledger.record_verified_processor_event("seed_capture", "seed_ref", "capture", 100)
        assert ledger.reserve_service(quote("seed_request", "seed_buyer", 100, "purchased", "seller"))
        assert ledger.settle_service("seed_request", "seller", 100, 0, "seed_decision", "a" * 64, 50, 50)
        assert ledger.release_earnings("seed_release", "seller", 100, "seed_risk")
        assert ledger.convert_earnings("seed_convert", "seller", 100)
        assert ledger.buyer_wallet("seller")["earned_access_available"] == 100

        denied(lambda: ledger.list_earned_credits("wrong_price", "seller", 20, 25, 2, expiry))
        assert ledger.list_earned_credits("listing_1", "seller", 20, 20, 2, expiry)
        assert not ledger.list_earned_credits("listing_1", "seller", 20, 20, 2, expiry)
        denied(lambda: ledger.place_resale_order("listing_1", "seller", "resale_ref_1"))

        # Listing and service admission compete for the same earned balance.
        def race_list():
            try:
                ledger.list_earned_credits("race_listing", "seller", 80, 80, 0, expiry)
                return "listing"
            except CommerceSimulationError:
                return None

        def race_use():
            try:
                ledger.reserve_service(quote("race_use", "seller", 80, "earned", "other_provider"))
                return "service"
            except CommerceSimulationError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(race_list), pool.submit(race_use)]
            results = [future.result() for future in results]
        assert sum(result is not None for result in results) == 1
        if "listing" in results:
            assert ledger.cancel_resale_listing("race_listing")
        else:
            assert ledger.refund_service("race_use", "race_fixture")

        # A reversal already known before capture never transfers credits.
        assert ledger.list_earned_credits("listing_void", "seller", 10, 10, 1, expiry)
        assert ledger.record_verified_resale_event("void_reversal", "resale_ref_void", "reversal", 10)
        assert ledger.record_verified_resale_event("void_capture", "resale_ref_void", "capture", 10)
        assert ledger.place_resale_order("listing_void", "buyer_void", "resale_ref_void")
        assert ledger.buyer_wallet("buyer_void")["resale_access_available"] == 0
        assert ledger.buyer_wallet("seller")["earned_access_available"] == 80
        denied(lambda: ledger.create_order("collision_void", "buyer_void", "resale_ref_void", 10))

        assert ledger.list_earned_credits("listing_cancel", "seller", 5, 5, 0, expiry)
        assert ledger.cancel_resale_listing("listing_cancel")
        assert not ledger.cancel_resale_listing("listing_cancel")
        denied(lambda: ledger.place_resale_order("listing_cancel", "buyer", "cancelled_ref"))
        assert ledger.list_earned_credits("listing_early_rev", "seller", 5, 5, 1, expiry)
        assert ledger.place_resale_order("listing_early_rev", "buyer_early", "resale_ref_early")
        assert ledger.record_verified_resale_event("early_capture", "resale_ref_early", "capture", 5)
        assert ledger.record_verified_resale_event("early_reversal", "resale_ref_early", "reversal", 5)
        assert ledger.buyer_wallet("buyer_early")["resale_access_available"] == 0
        assert ledger.buyer_wallet("seller")["earned_access_available"] == 80
        assert ledger.audit()["unfunded_reversal_loss"] == 0
        denied(lambda: ledger.release_resale_proceeds("too_late", "listing_early_rev", "risk_early"))

        assert ledger.place_resale_order("listing_1", "buyer", "resale_ref_1")
        denied(lambda: ledger.create_order("collision_sale", "buyer", "resale_ref_1", 20))
        denied(lambda: ledger.record_verified_processor_event("wrong_kind", "resale_ref_1", "capture", 20))
        assert ledger.record_verified_resale_event("sale_capture", "resale_ref_1", "capture", 20)
        assert not ledger.record_verified_resale_event("sale_capture", "resale_ref_1", "capture", 20)
        assert ledger.record_verified_resale_event("sale_capture_duplicate", "resale_ref_1", "capture", 20)
        denied(lambda: ledger.record_verified_resale_event("sale_capture", "resale_ref_1", "capture", 21))
        assert ledger.buyer_wallet("buyer")["resale_access_available"] == 20
        assert ledger.provider_wallet("seller")["resale_payable_held"] == 18

        assert ledger.list_earned_credits("listing_other", "seller", 5, 5, 0, expiry)
        assert ledger.place_resale_order("listing_other", "buyer", "resale_ref_other")
        assert ledger.record_verified_resale_event("other_capture", "resale_ref_other", "capture", 5)
        assert ledger.buyer_wallet("buyer")["resale_access_available"] == 25
        assert ledger.resale_lot_balance("buyer", "listing_1") == 20
        assert ledger.resale_lot_balance("buyer", "listing_other") == 5
        denied(lambda: ledger.reserve_service(
            quote("wrong_buyer", "buyer_void", 5, "resale", "service_provider", "listing_other")
        ))

        # Resale-origin access can be used but cannot itself be listed.
        denied(lambda: ledger.list_earned_credits("buyer_cannot_resell", "buyer", 20, 20, 0, expiry))
        assert ledger.reserve_service(quote("buyer_use", "buyer", 5, "resale", "service_provider", "listing_1"))
        assert ledger.settle_service("buyer_use", "service_provider", 5, 0, "use_decision", "b" * 64, 2, 3)
        assert ledger.buyer_wallet("buyer")["resale_access_available"] == 20
        assert ledger.resale_lot_balance("buyer", "listing_1") == 15
        denied(lambda: ledger.reserve_service(
            quote("wrong_lot_use", "buyer", 20, "resale", "service_provider", "listing_1")
        ))

        # A reversal while a buyer request is held must route its later refund
        # into loss recovery, never recreate spendable buyer credits.
        assert ledger.list_earned_credits("listing_held", "seller", 10, 10, 0, expiry)
        assert ledger.place_resale_order("listing_held", "buyer_held", "resale_ref_held")
        assert ledger.record_verified_resale_event("held_capture", "resale_ref_held", "capture", 10)
        assert ledger.reserve_service(quote(
            "held_use", "buyer_held", 10, "resale", "service_provider", "listing_held"
        ))
        assert ledger.record_verified_resale_event("held_reversal", "resale_ref_held", "reversal", 10)
        assert ledger.audit()["unfunded_reversal_loss"] == 10
        assert ledger.refund_service("held_use", "no_output")
        assert ledger.audit()["unfunded_reversal_loss"] == 0
        assert ledger.resale_lot_balance("buyer_held", "listing_held") == 0
        assert ledger.audit()["unfunded_reversal_loss"] == 0

    with CommerceSimulator(path) as ledger:
        denied(lambda: ledger.release_resale_proceeds("seed_release", "listing_1", "other_risk"))
        assert ledger.release_resale_proceeds("resale_release", "listing_1", "resale_risk")
        assert not ledger.release_resale_proceeds("resale_release", "listing_1", "resale_risk")
        denied(lambda: ledger.release_resale_proceeds("other_release", "listing_1", "other_risk"))
        denied(lambda: ledger.release_earnings("resale_release", "service_provider", 5, "different_risk"))
        assert ledger.provider_wallet("seller")["eligible"] == 18
        assert ledger.request_payout("resale_payout", "seller", 10, "resale_external")
        assert ledger.resolve_payout("resale_payout", "resale_paid", paid=True)
        assert ledger.record_verified_resale_event("sale_reversal", "resale_ref_1", "reversal", 20)
        assert not ledger.record_verified_resale_event("sale_reversal", "resale_ref_1", "reversal", 20)
        assert ledger.record_verified_resale_event("sale_reversal_duplicate", "resale_ref_1", "reversal", 20)
        assert ledger.resale_lot_balance("buyer", "listing_1") == 0
        assert ledger.resale_lot_balance("buyer", "listing_other") == 5
        assert ledger.buyer_wallet("seller")["earned_access_available"] == 95
        assert ledger.provider_wallet("seller")["eligible"] == 0
        assert ledger.audit()["unfunded_reversal_loss"] == 15
        assert ledger.record_verified_resale_event("other_reversal", "resale_ref_other", "reversal", 5)
        assert ledger.buyer_wallet("buyer")["resale_access_available"] == 0
        assert ledger.buyer_wallet("seller")["earned_access_available"] == 100
        assert ledger.audit()["unfunded_reversal_loss"] == 15
        denied(lambda: ledger.list_earned_credits("blocked_listing", "seller", 1, 1, 0, expiry))
        denied(lambda: ledger.reserve_service(quote("blocked_use", "seller", 1, "earned", "other")))

    # The audit must compare canonical resale postings against materialized rows.
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("UPDATE resale_listings SET fee=3 WHERE listing_id='listing_1'")
    with CommerceSimulator(path) as tampered:
        denied(tampered.audit)


def check_migration(path):
    sample = quote("legacy_request", "legacy_buyer", 10, "purchased", "legacy_provider")
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "CREATE TABLE reservations (request_id TEXT PRIMARY KEY,buyer_id TEXT NOT NULL,"
            "source TEXT NOT NULL CHECK(source IN ('purchased','earned')),cap INTEGER NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('held','settled','refunded')),provider_id TEXT,"
            "charge INTEGER,fee INTEGER,decision_id TEXT UNIQUE,receipt_digest TEXT,"
            "refund_reason TEXT,input_units INTEGER,output_units INTEGER)"
        )
        connection.execute(
            "CREATE TABLE quotes (request_id TEXT PRIMARY KEY REFERENCES reservations(request_id),"
            "terms_json TEXT NOT NULL,terms_digest TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO reservations(request_id,buyer_id,source,cap,status) VALUES (?,?,?,?,?)",
            (sample.request_id, sample.buyer_id, sample.funding_source, sample.spend_cap, "held"),
        )
        connection.execute("INSERT INTO quotes VALUES (?,?,?)",
                           (sample.request_id, json.dumps(asdict(sample), sort_keys=True,
                            separators=(",", ":"), ensure_ascii=True), sample.digest))
        connection.execute("PRAGMA user_version=1")
    connection.close()
    with CommerceSimulator(path) as upgraded:
        assert upgraded._load_quote("legacy_request").digest == sample.digest
        assert upgraded._db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert upgraded._db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "'resale'" in upgraded._db.execute(
            "SELECT sql FROM sqlite_master WHERE name='reservations'"
        ).fetchone()[0]


def check_held_settlement(path):
    expiry = int(time.time()) + 60
    with CommerceSimulator(path) as ledger:
        ledger.create_order("fund", "seed", "fund_ref", 10)
        ledger.record_verified_processor_event("fund_capture", "fund_ref", "capture", 10)
        ledger.reserve_service(quote("fund_work", "seed", 10, "purchased", "seller"))
        ledger.settle_service("fund_work", "seller", 10, 0, "fund_decision", "a" * 64, 5, 5)
        ledger.release_earnings("fund_release", "seller", 10, "fund_risk")
        ledger.convert_earnings("fund_convert", "seller", 10)
        ledger.list_earned_credits("lot", "seller", 10, 10, 0, expiry)
        ledger.place_resale_order("lot", "buyer", "lot_ref")
        ledger.record_verified_resale_event("lot_capture", "lot_ref", "capture", 10)
        ledger.reserve_service(quote("in_flight", "buyer", 10, "resale", "worker", "lot"))
        ledger.record_verified_resale_event("lot_reversal", "lot_ref", "reversal", 10)
        assert ledger.audit()["unfunded_reversal_loss"] == 10
        ledger.settle_service("in_flight", "worker", 5, 0, "work_decision", "b" * 64, 2, 3)
        assert ledger.audit()["unfunded_reversal_loss"] == 5
        assert ledger.resale_lot_balance("buyer", "lot") == 0


def main():
    with tempfile.TemporaryDirectory(prefix="communityai-resale-") as temporary:
        check_resale(Path(temporary) / "resale.db")
        check_migration(Path(temporary) / "legacy.db")
        check_held_settlement(Path(temporary) / "held.db")
    print("PASS: durable noncash resale, replay, race, reversal loss and v1 migration")


if __name__ == "__main__":
    main()
