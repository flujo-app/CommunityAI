"""Provider-neutral orchestration for the one-click Gate 13 replay.

The provider adapter owns cloud-specific resource operations.  This module owns the
qualification order, durable local evidence, failure handling, and the invariant that
cleanup is attempted after every run which reaches cloud mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


SCHEMA_VERSION = 1
PLATFORMS = ("windows", "linux")
TERMINAL_RESULTS = frozenset({"passed", "failed"})


class Gate13CloudError(RuntimeError):
    """The one-click qualification could not complete safely."""


@dataclass(frozen=True)
class PackageArtifact:
    """Public identity of one locally verified production package."""

    platform: str
    source_commit: str
    workflow_run_id: int
    artifact_id: int
    artifact_name: str
    wrapper_sha256: str
    wrapper_bytes: int
    archive_name: str
    archive_sha256: str
    archive_bytes: int

    def public_record(self) -> dict[str, Any]:
        return asdict(self)


class PackageSource(Protocol):
    """Build or resolve the exact production packages for one run."""

    def prepare(self) -> Mapping[str, PackageArtifact]: ...


class CloudProvider(Protocol):
    """Cloud boundary used by the provider-neutral lifecycle."""

    name: str

    def preflight(self) -> Mapping[str, Any]: ...

    def create_route(self) -> None: ...

    def prepare_route(self) -> Mapping[str, Any]: ...

    def fence_route(self, platform: str) -> Mapping[str, Any]: ...

    def create_client(self, platform: str, package: PackageArtifact) -> None: ...

    def prepare_client(self, platform: str, package: PackageArtifact) -> Mapping[str, Any]: ...

    def run_client(self, platform: str, package: PackageArtifact) -> bytes: ...

    def delete_client(self, platform: str) -> None: ...

    def delete_route(self) -> None: ...

    def cleanup_all(self) -> Mapping[str, Any]: ...

    def verify_cleanup(self) -> Mapping[str, Any]: ...


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(value)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _require_passed(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    result = dict(value)
    if result.get("result") != "passed":
        raise Gate13CloudError(f"{label} did not pass")
    return result


def _validate_platforms(packages: Mapping[str, PackageArtifact]) -> dict[str, PackageArtifact]:
    if set(packages) != set(PLATFORMS):
        raise Gate13CloudError("package source did not return Windows and Linux")
    result = dict(packages)
    commits = {item.source_commit for item in result.values()}
    if len(commits) != 1:
        raise Gate13CloudError("production packages do not share one source commit")
    for platform in PLATFORMS:
        package = result[platform]
        if package.platform != platform:
            raise Gate13CloudError("production package platform changed")
        if any(
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in (package.wrapper_sha256, package.archive_sha256)
        ):
            raise Gate13CloudError("production package digest is invalid")
        if package.wrapper_bytes <= 0 or package.archive_bytes <= 0:
            raise Gate13CloudError("production package byte size is invalid")
    return result


class RunRecorder:
    """Small durable journal intended for a person, retry logic, and later evidence."""

    def __init__(self, run_id: str, provider: str, output_root: Path, clock: Callable[[], float]) -> None:
        self.run_id = run_id
        self.provider = provider
        self.output_root = output_root
        self.clock = clock
        self.started_at_unix = int(clock())
        self.current_phase = "INITIALIZING"
        self.events: list[dict[str, Any]] = []
        self.package_records: dict[str, Any] = {}
        self.route_fences: dict[str, Any] = {}
        self.client_evidence: dict[str, Any] = {}
        self.cleanup: dict[str, Any] | None = None
        self.result: str | None = None
        self.failure_code: str | None = None
        self._persist()

    @property
    def state_path(self) -> Path:
        return self.output_root / "run-state.json"

    def phase(self, name: str, **details: Any) -> None:
        self.current_phase = name
        event = {"phase": name, "recorded_at_unix": int(self.clock())}
        if details:
            event["details"] = details
        self.events.append(event)
        self._persist()

    def _document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": "gate13-one-click-cloud-run-state",
            "run_id": self.run_id,
            "provider": self.provider,
            "started_at_unix": self.started_at_unix,
            "updated_at_unix": int(self.clock()),
            "phase": self.current_phase,
            "events": list(self.events),
            "packages": dict(self.package_records),
            "route_fences": dict(self.route_fences),
            "clients": dict(self.client_evidence),
            "cleanup": self.cleanup,
            "result": self.result,
            "failure_code": self.failure_code,
        }

    def _persist(self) -> None:
        _atomic_json(self.state_path, self._document())

    def finish(self, result: str, *, failure_code: str | None = None) -> dict[str, Any]:
        if result not in TERMINAL_RESULTS:
            raise Gate13CloudError("terminal result is invalid")
        self.result = result
        self.failure_code = failure_code
        self.current_phase = "COMPLETE" if result == "passed" else "FAILED"
        document = self._document()
        document["finished_at_unix"] = int(self.clock())
        document["duration_seconds"] = document["finished_at_unix"] - self.started_at_unix
        _atomic_json(self.output_root / "result.json", document)
        self._persist()
        return document


class Gate13CloudOrchestrator:
    """Run the exact route -> Windows -> Linux -> cleanup sequence."""

    def __init__(
        self,
        *,
        run_id: str,
        package_source: PackageSource,
        provider: CloudProvider,
        output_root: Path,
        evidence_validator: Callable[[str, bytes, PackageArtifact], Mapping[str, Any]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.run_id = run_id
        self.package_source = package_source
        self.provider = provider
        self.output_root = output_root
        self.evidence_validator = evidence_validator
        self.clock = clock

    def run(self) -> Mapping[str, Any]:
        recorder = RunRecorder(self.run_id, self.provider.name, self.output_root, self.clock)
        cloud_mutated = False
        failure_code: str | None = None
        try:
            recorder.phase("PREFLIGHT")
            _require_passed(self.provider.preflight(), "cloud preflight")
            recorder.phase("PACKAGES_RESOLVING")
            packages = _validate_platforms(self.package_source.prepare())
            recorder.package_records = {
                platform: packages[platform].public_record() for platform in PLATFORMS
            }

            # From this point onward every exit path must execute provider cleanup.
            cloud_mutated = True
            recorder.phase("ROUTE_CREATING")
            self.provider.create_route()
            recorder.phase("ROUTE_PREPARING")
            _require_passed(self.provider.prepare_route(), "route preparation")

            for platform in PLATFORMS:
                recorder.phase("ROUTE_FENCING", platform=platform)
                fence = _require_passed(self.provider.fence_route(platform), f"{platform} route fence")
                recorder.route_fences[platform] = fence
                recorder.phase("CLIENT_CREATING", platform=platform)
                self.provider.create_client(platform, packages[platform])
                recorder.phase("CLIENT_PREPARING", platform=platform)
                _require_passed(
                    self.provider.prepare_client(platform, packages[platform]),
                    f"{platform} client preparation",
                )
                recorder.phase("CLIENT_RUNNING", platform=platform)
                evidence_payload = self.provider.run_client(platform, packages[platform])
                validated = _require_passed(
                    self.evidence_validator(platform, evidence_payload, packages[platform]),
                    f"{platform} qualification",
                )
                evidence_path = self.output_root / f"{platform}-evidence.json"
                _atomic_bytes(evidence_path, evidence_payload)
                recorder.client_evidence[platform] = {
                    "result": "passed",
                    "sha256": "sha256:" + hashlib.sha256(evidence_payload).hexdigest(),
                    "session_duration_seconds": validated.get("session_duration_seconds"),
                }
                recorder.phase("CLIENT_DELETING", platform=platform)
                self.provider.delete_client(platform)

            recorder.phase("ROUTE_DELETING")
            self.provider.delete_route()
        except BaseException as exc:
            failure_code = type(exc).__name__
            failed_phase = recorder.current_phase
            recorder.phase(
                "FAILURE",
                failed_phase=failed_phase,
                failure_code=failure_code,
            )
        finally:
            if cloud_mutated:
                recorder.phase("CLEANUP")
                try:
                    recorder.cleanup = dict(self.provider.cleanup_all())
                    if recorder.cleanup.get("result") != "passed":
                        failure_code = failure_code or "CleanupError"
                except BaseException as cleanup_exc:
                    recorder.cleanup = {
                        "result": "failed",
                        "failure_code": type(cleanup_exc).__name__,
                    }
                    failure_code = failure_code or "CleanupError"

        try:
            recorder.phase("CLEANUP_VERIFYING")
            verified_cleanup = _require_passed(self.provider.verify_cleanup(), "cloud cleanup")
            recorder.cleanup = verified_cleanup
        except BaseException as cleanup_exc:
            failure_code = failure_code or type(cleanup_exc).__name__
            recorder.cleanup = {
                "result": "failed",
                "failure_code": type(cleanup_exc).__name__,
            }

        passed = (
            failure_code is None
            and set(recorder.client_evidence) == set(PLATFORMS)
            and isinstance(recorder.cleanup, dict)
            and recorder.cleanup.get("result") == "passed"
        )
        return recorder.finish("passed" if passed else "failed", failure_code=failure_code)
