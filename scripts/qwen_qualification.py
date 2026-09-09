"""Gate 13's durable phase recording around the established Qwen cloud/client test."""

import sys
import time

from gate13_cloud_orchestrator import Gate13CloudError, RunRecorder
from gate13_gcp_provider import LoggedRunner
from qwen_product_provenance import sha256, snapshot, verify_package, verify_snapshot
from report_qwen_product import report
from run_gate13_gcp import _write_json
from run_qwen_product_mixed import MixedProductRun
from run_qwen_product_test import execute_packaged


class QwenRecorder(RunRecorder):
    def _document(self):
        value = super()._document()
        value["scope"] = "qwen-one-click-source-and-packaged-qualification"
        return value

    def phase(self, name, **details):
        super().phase(name, **details)
        print(f"[{time.strftime('%H:%M:%S')}] {name}", flush=True)


class QualificationRun(MixedProductRun):
    """Add phase boundaries and an inline Windows client; reuse cloud lifecycle/cleanup."""

    def __init__(self, path, cloud_config, *, root, packaged_config, inputs, inventory, recorder):
        super().__init__(path, cloud_config)
        self.root = root
        self.packaged_config = packaged_config
        self.inputs = inputs
        self.inventory = inventory
        self.recorder = recorder
        self.cloud_creation_reached = False
        self.packaged_exit_code = None
        self.package_verification = None
        self.runner = LoggedRunner(path / "command-journal.jsonl")

    def event(self, phase, **details):
        if phase == "failed" and not any(e["phase"] == "FAILURE" for e in self.recorder.events):
            self.recorder.phase(
                "FAILURE",
                failed_phase=self.recorder.current_phase,
                failure_reason=details.get("error", "Cloud test failed"),
            )
        super().event(phase, **details)

    def preflight(self):
        self.recorder.phase("PREFLIGHT")
        super().preflight()
        self.recorder.phase("PACKAGES_VERIFYING")
        self.runner.run(
            [sys.executable, "-c", "import httpx, psutil, communityai_desktop.controller"],
            action="Checking the Qwen desktop test runtime",
            timeout=60,
        )
        self.package_verification = verify_package(
            self.inputs["node"], self.inputs["package_provenance"], self.packaged_config["node_sha256"]
        )
        self.recorder.package_records["windows"] = {
            **self.package_verification,
            "provenance_sha256": sha256(self.inputs["package_provenance"]),
            "scope": "explicit verified engineering package; no current-HEAD build claim",
        }
        verify_snapshot(self.root, self.path, self.inventory)

    def bundle(self):
        self.recorder.phase("SOURCE_BUNDLING")
        super().bundle()
        verify_snapshot(self.root, self.path, self.inventory)

    def create_firewalls(self):
        self.cloud_creation_reached = True  # Set before the first resource mutation.
        self.recorder.phase("ROUTE_CREATING")
        super().create_firewalls()

    def stage(self, name, span=None, peers=()):
        self.recorder.phase("ROUTE_PREPARING", instance=name)
        super().stage(name, span, peers)

    def exercise_workers(self):
        self.recorder.phase("SOURCE_RUNNING")
        try:
            return super().exercise_workers()
        except BaseException as exc:
            # A failed source run may still collect an independent packaged
            # result. Preserve the original failed phase across that later step.
            self.recorder.phase("FAILURE", failed_phase="SOURCE_RUNNING", failure_reason=str(exc))
            raise

    def run_packaged_client(self):
        # Called after the owned ready/deadline receipt is written, inside run()'s
        # try/finally. No background orchestrator or manual client launch is needed.
        self.recorder.phase("CLIENT_RUNNING", platform="windows")
        verify_snapshot(self.root, self.path, self.inventory)
        self.packaged_exit_code = execute_packaged(self.path, self.path / "packaged", self.packaged_config, self.inputs)

    def cleanup(self):
        self.recorder.phase("CLEANUP")
        return super().cleanup()


class QwenQualification:
    def __init__(self, *, root, output_root, cloud_config, packaged_config, inputs):
        self.root = root
        self.output_root = output_root
        self.cloud_config = cloud_config
        self.packaged_config = packaged_config
        self.inputs = inputs

    def run(self):
        path = self.output_root
        # Keep the established raw cloud result.json intact. Gate 13's recorder
        # writes the aggregate into a separate directory within this fresh run.
        recorder = QwenRecorder(path.name, "gcp-and-azure", path / "qualification", time.time)
        value = {"result": "failed", "run_id": path.name}
        run = None
        raw = {}
        failure_code = None
        failure_reason = None
        try:
            recorder.phase("SOURCE_RECORDING")
            inventory = snapshot(self.root, path, self.inputs)
            value["source_inventory_sha256"] = sha256(path / "launcher-source.json")
            run = QualificationRun(
                path,
                self.cloud_config,
                root=self.root,
                packaged_config=self.packaged_config,
                inputs=self.inputs,
                inventory=inventory,
                recorder=recorder,
            )
            raw = run.run()  # The proven cloud runner attempts cleanup in finally, on every failure.
            value.update(
                cloud_result=raw,
                package_verification=run.package_verification,
                packaged_exit_code=run.packaged_exit_code,
            )
            if raw.get("result") != "passed":
                raise Gate13CloudError(raw.get("error") or raw.get("cleanup_error") or "Qwen cloud/client test failed")
            if run.packaged_exit_code != 0:
                raise Gate13CloudError("The packaged client did not complete successfully")
            recorder.phase("INPUTS_VERIFYING")
            verify_snapshot(self.root, path, inventory)
            verify_package(self.inputs["node"], self.inputs["package_provenance"], self.packaged_config["node_sha256"])
            value.update(result="passed", inputs_unchanged=True)
        except BaseException as exc:
            failure_code, failure_reason = type(exc).__name__, str(exc) or type(exc).__name__
            value["error"] = failure_reason
            if not any(e["phase"] == "FAILURE" for e in recorder.events):
                recorder.phase("FAILURE", failed_phase=recorder.current_phase, failure_reason=failure_reason)
        finally:
            _write_json(path / "launcher-result.json", value)

        recorder.phase("CLEANUP_VERIFYING")
        cleanup = raw.get("cleanup", {})
        if cleanup.get("verified") is True:
            recorder.cleanup = {"result": "passed", "evidence": cleanup}
        elif run is None or not run.cloud_creation_reached:
            recorder.cleanup = {"result": "not-needed", "reason": "This run did not reach cloud resource creation"}
        else:
            recorder.cleanup = {"result": "failed", "evidence": cleanup}
            failure_code = failure_code or "CleanupError"
            failure_reason = failure_reason or "Owned cloud cleanup was not verified"

        recorder.phase("REPORT_VALIDATING")
        try:
            summary = report(path, path / "packaged", path / "product-report.json", launcher=value)
            if summary["result"] != "passed":
                raise Gate13CloudError(summary.get("error", "Qwen evidence validation failed"))
            recorder.client_evidence["windows"] = {
                "result": "passed",
                "sha256": "sha256:" + sha256(path / "packaged/result.json"),
            }
        except BaseException as exc:
            failure_code = failure_code or type(exc).__name__
            failure_reason = failure_reason or str(exc) or type(exc).__name__
            if not any(e["phase"] == "FAILURE" for e in recorder.events):
                recorder.phase("FAILURE", failed_phase="REPORT_VALIDATING", failure_reason=failure_reason)
        passed = (
            failure_code is None
            and value["result"] == "passed"
            and recorder.cleanup.get("result") == "passed"
            and "windows" in recorder.client_evidence
        )
        return recorder.finish(
            "passed" if passed else "failed", failure_code=failure_code, failure_reason=failure_reason
        )
