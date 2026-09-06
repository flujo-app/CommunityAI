#!/usr/bin/env python3
"""Zero-input Qwen qualification launcher, following run_gate13_gcp.py's structure."""

import os
import secrets
import sys
import time
from pathlib import Path

from gate13_cloud_orchestrator import Gate13CloudError
from qwen_qualification import QwenQualification
from run_gate13_gcp import _write_json
from run_qwen_full_inference_gcp import LauncherLock
from run_qwen_mixed_inference import require_cpu_proof
from run_qwen_product_test import load_inputs, read


def _new_run_id():
    return time.strftime("q38pm-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)


def _inputs(root):
    packaged, inputs = load_inputs(root / "config/qwen_product_test.json", root)
    proof = None
    for candidate in sorted((root / ".gate13-runs/qwen-full").glob("q38-*"), reverse=True):
        try:
            require_cpu_proof(candidate)
        except (OSError, KeyError, TypeError, ValueError):
            continue
        proof = candidate
        break
    if proof is None:
        raise Gate13CloudError("A passed CPU inference/recovery/cleanup proof is required before this mixed test")
    inputs.update(cpu_proof=proof / "result.json", cpu_replacement=proof / "replacement.json")
    cloud = read(inputs["cloud_config"])
    cloud.update(
        cpu_proof_path=str(proof.resolve()),
        worker_machine_type="c3-highmem-4",
        max_duration_seconds=10800,
        packaged_client_wait_seconds=3600,
    )
    return packaged, inputs, cloud


def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv):
        print("This launcher accepts no arguments.", file=sys.stderr)
        return 2
    repository_root = Path(__file__).resolve().parent.parent
    runs_root = repository_root / ".gate13-runs/qwen-product-mixed"
    try:
        if os.name == "nt" and repository_root.drive.upper() != "C:":
            raise Gate13CloudError("This workspace's Qwen qualification must run from C:")
        with LauncherLock(runs_root / "launcher.lock"):
            run_id = _new_run_id()
            output_root = runs_root / run_id
            output_root.mkdir(parents=True, exist_ok=False)
            packaged, inputs, config = _inputs(repository_root)
            _write_json(output_root / "provider-config.json", config)
            _write_json(output_root / "requested-provider-config.json", config)
            inputs["requested_provider_config"] = output_root / "requested-provider-config.json"
            result = QwenQualification(
                root=repository_root,
                output_root=output_root,
                cloud_config=config,
                packaged_config=packaged,
                inputs=inputs,
            ).run()
            print()
            print("=" * 68)
            print(f"QWEN QUALIFICATION: {str(result.get('result')).upper()}")
            print(f"Run: {run_id}")
            print(f"Duration: {result.get('duration_seconds')} seconds")
            if result.get("result") != "passed":
                failure = next(
                    (
                        event.get("details", {}).get("failed_phase")
                        for event in result.get("events", [])
                        if event.get("phase") == "FAILURE"
                    ),
                    None,
                )
                print(f"Failed phase: {failure or 'cleanup verification'}")
                print(f"Reason: {result.get('failure_reason') or result.get('failure_code') or 'unknown'}")
            print(f"Result: {output_root / 'qualification/result.json'}")
            print(f"Evidence: {output_root / 'product-report.json'}")
            print("=" * 68)
            return 0 if result.get("result") == "passed" else 1
    except BaseException as exc:
        print()
        print("=" * 68)
        print("QWEN QUALIFICATION: FAILED BEFORE OR DURING ORCHESTRATION")
        print(f"Failure: {type(exc).__name__}: {exc}")
        print(f"Runs directory: {runs_root}")
        print("=" * 68)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
