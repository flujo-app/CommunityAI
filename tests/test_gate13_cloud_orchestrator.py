import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_cloud_orchestrator as cloud


def package(platform):
    archive = "communityai-desktop-windows.zip" if platform == "windows" else "communityai-desktop-linux.tar.gz"
    return cloud.PackageArtifact(
        platform=platform,
        source_commit="a" * 40,
        workflow_run_id=123,
        artifact_id=456 if platform == "windows" else 457,
        artifact_name=f"communityai-desktop-install-{platform}",
        wrapper_sha256="c" * 64,
        wrapper_bytes=110,
        archive_name=archive,
        archive_sha256="b" * 64,
        archive_bytes=100,
    )


class Packages:
    def __init__(self):
        self.prepare_calls = 0

    def prepare(self):
        self.prepare_calls += 1
        return {platform: package(platform) for platform in cloud.PLATFORMS}


class Provider:
    name = "fake"

    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []

    def _call(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise RuntimeError(name)

    def preflight(self):
        self._call("preflight")
        return {"result": "passed"}

    def create_route(self):
        self._call("create_route")

    def prepare_route(self):
        self._call("prepare_route")
        return {"result": "passed"}

    def fence_route(self, platform):
        self._call(f"fence_{platform}")
        return {"result": "passed", "target": platform}

    def create_client(self, platform, package):
        self._call(f"create_{platform}")

    def prepare_client(self, platform, package):
        self._call(f"prepare_{platform}")
        return {"result": "passed"}

    def run_client(self, platform, package):
        self._call(f"run_{platform}")
        return json.dumps({"result": "passed", "platform": platform}).encode()

    def delete_client(self, platform):
        self._call(f"delete_{platform}")

    def delete_route(self):
        self._call("delete_route")

    def cleanup_all(self):
        self._call("cleanup_all")
        if self.fail_at == "cleanup_result":
            return {"result": "failed"}
        return {"result": "passed"}

    def verify_cleanup(self):
        self._call("verify_cleanup")
        return {"result": "passed", "all_absent": True}


def validate(platform, payload, package):
    value = json.loads(payload)
    assert value["platform"] == platform
    assert package.platform == platform
    return value


def test_complete_sequence_is_ordered_and_persisted(tmp_path):
    provider = Provider()
    result = cloud.Gate13CloudOrchestrator(
        run_id="gate13-test-a",
        package_source=Packages(),
        provider=provider,
        output_root=tmp_path,
        evidence_validator=validate,
        clock=lambda: 100,
    ).run()

    assert result["result"] == "passed"
    assert provider.calls == [
        "preflight",
        "create_route",
        "prepare_route",
        "fence_windows",
        "create_windows",
        "prepare_windows",
        "run_windows",
        "delete_windows",
        "fence_linux",
        "create_linux",
        "prepare_linux",
        "run_linux",
        "delete_linux",
        "delete_route",
        "cleanup_all",
        "verify_cleanup",
    ]
    assert (tmp_path / "windows-evidence.json").is_file()
    assert (tmp_path / "linux-evidence.json").is_file()
    assert json.loads((tmp_path / "result.json").read_text())["cleanup"]["all_absent"] is True


@pytest.mark.parametrize(
    "failure",
    [
        "create_route",
        "prepare_route",
        "fence_windows",
        "create_windows",
        "prepare_windows",
        "run_windows",
        "delete_windows",
        "fence_linux",
        "create_linux",
        "prepare_linux",
        "run_linux",
        "delete_linux",
        "delete_route",
    ],
)
def test_every_cloud_failure_attempts_cleanup_and_verifies_absence(tmp_path, failure):
    provider = Provider(fail_at=failure)
    result = cloud.Gate13CloudOrchestrator(
        run_id="gate13-test-a",
        package_source=Packages(),
        provider=provider,
        output_root=tmp_path,
        evidence_validator=validate,
        clock=lambda: 100,
    ).run()

    assert result["result"] == "failed"
    assert result["failure_reason"]
    assert "cleanup_all" in provider.calls
    assert provider.calls[-1] == "verify_cleanup"


def test_preflight_failure_does_not_mutate_but_still_verifies_absence(tmp_path):
    provider = Provider(fail_at="preflight")
    packages = Packages()
    result = cloud.Gate13CloudOrchestrator(
        run_id="gate13-test-a",
        package_source=packages,
        provider=provider,
        output_root=tmp_path,
        evidence_validator=validate,
        clock=lambda: 100,
    ).run()

    assert result["result"] == "failed"
    assert provider.calls == ["preflight", "verify_cleanup"]
    assert packages.prepare_calls == 0


def test_cleanup_failure_result_cannot_be_overwritten_by_successful_verification(tmp_path):
    provider = Provider(fail_at="cleanup_result")
    result = cloud.Gate13CloudOrchestrator(
        run_id="gate13-test-a",
        package_source=Packages(),
        provider=provider,
        output_root=tmp_path,
        evidence_validator=validate,
        clock=lambda: 100,
    ).run()

    assert result["result"] == "failed"
    assert result["failure_code"] == "CleanupError"
