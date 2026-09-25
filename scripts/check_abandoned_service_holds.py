"""Subsecond durable simulated-hold inspection and stop-confirmed recovery."""

import importlib.util
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "src" / "drift" / "commerce_simulator.py"
spec = importlib.util.spec_from_file_location("commerce_simulator_holds", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def quote(request_id):
    return module.SimulatedServiceQuote(
        request_id=request_id, buyer_id="buyer", provider_id="provider",
        funding_source="purchased", model_id="test/model", profile_id="test/profile",
        service_class="text_inference", settlement_domain="local_simulation",
        artifact_sha256="a" * 64, service_policy_sha256="b" * 64,
        price_schedule_sha256="c" * 64, input_unit_price=1, output_unit_price=1,
        max_input_units=5, max_output_units=5, fee_bps=0, spend_cap=10,
        expires_at_unix=int(time.time()) + 60,
    )


def main():
    with tempfile.TemporaryDirectory(prefix="communityai-held-restore-") as temporary:
        path = Path(temporary) / "journal.db"
        with module.CommerceSimulator(path) as journal:
            journal.create_order("order", "buyer", "processor", 20)
            journal.record_verified_processor_event("capture", "processor", "capture", 20)
            journal.reserve_service(quote("orphan"))
            assert journal.buyer_wallet("buyer")["service_held"] == 10
        with module.CommerceSimulator(path) as journal:
            assert journal.unresolved_service_holds(limit=1) == (quote("orphan"),)
            try:
                journal.unresolved_service_holds(limit=0)
            except module.CommerceSimulationError:
                pass
            else:
                raise AssertionError("unbounded recovery inspection accepted")
            # The caller's process-stop proof is outside this synthetic journal.
            assert journal.refund_service("orphan", "confirmed_stop")
            assert journal.unresolved_service_holds() == ()
            assert journal.buyer_wallet("buyer")["purchased_available"] == 20

            journal.reserve_service(quote("race"))

            def settle():
                try:
                    return journal.settle_service("race", "provider", 5, 0,
                                                  "decision", "d" * 64, 2, 3)
                except module.CommerceSimulationError:
                    return False

            def refund():
                try:
                    return journal.refund_service("race", "confirmed_stop")
                except module.CommerceSimulationError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [pool.submit(settle), pool.submit(refund)]
                assert sum(task.result() is True for task in results) == 1
            assert journal.unresolved_service_holds() == ()
            assert journal.audit()["unfunded_reversal_loss"] == 0
    print("PASS: durable hold inspection, explicit refund and settlement/refund race")


if __name__ == "__main__":
    main()
