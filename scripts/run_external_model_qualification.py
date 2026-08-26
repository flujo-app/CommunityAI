"""Run one exact candidate on a pre-provisioned external qualification host.

This is the narrow adapter used by the manual self-hosted GitHub Actions matrix.
Artifact locations and opaque machine identity come from the runner environment so
host-local paths do not become workflow inputs or shared evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

if __package__:
    from scripts.qualify_model_manifest import infer_source_commit, main as qualify_main
else:
    from qualify_model_manifest import infer_source_commit, main as qualify_main

from drift.model_manifest import ManifestError, ModelManifest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Candidate:
    manifest: Path
    artifact_root_environment: str
    cache_dir_environment: str


@dataclass(frozen=True)
class HostProfile:
    system: str
    device: str


CANDIDATES: Mapping[str, Candidate] = {
    "qwen3.5-2b": Candidate(
        manifest=REPOSITORY_ROOT / "manifests" / "candidates" / "qwen3.5-2b-bfloat16-eager.json",
        artifact_root_environment="COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT",
        cache_dir_environment="COMMUNITYAI_QWEN35_2B_CACHE_DIR",
    ),
    "gemma-4-e2b": Candidate(
        manifest=REPOSITORY_ROOT / "manifests" / "candidates" / "gemma-4-e2b-it-bfloat16-eager.json",
        artifact_root_environment="COMMUNITYAI_GEMMA4_E2B_ARTIFACT_ROOT",
        cache_dir_environment="COMMUNITYAI_GEMMA4_E2B_CACHE_DIR",
    ),
}
HOST_PROFILES: Mapping[str, HostProfile] = {
    "windows-cpu": HostProfile(system="windows", device="cpu"),
    "windows-cuda": HostProfile(system="windows", device="cuda"),
    "linux-cpu": HostProfile(system="linux", device="cpu"),
    "linux-cuda": HostProfile(system="linux", device="cuda"),
    "macos-cpu": HostProfile(system="macos", device="cpu"),
    "macos-mps": HostProfile(system="macos", device="mps"),
}


class ExternalQualificationError(ValueError):
    """The external runner is not provisioned for its claimed qualification profile."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a release-candidate local gate on one explicitly labelled external host",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--candidate", required=True, choices=tuple(CANDIDATES))
    parser.add_argument("--profile", required=True, choices=tuple(HOST_PROFILES))
    parser.add_argument("--source-commit", help="Exact checkout commit; defaults to GITHUB_SHA")
    parser.add_argument("--timeout", type=float, default=7200, help="Timeout for each parity/failover smoke")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate the host label, private paths, source identity, and device without producing qualification evidence",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Bounded local qualification report; required unless --preflight-only is used",
    )
    return parser


def normalize_system() -> str:
    observed = platform.system().strip().lower()
    return {"darwin": "macos"}.get(observed, observed)


def require_environment(
    candidate: Candidate,
    profile: HostProfile,
    *,
    source_commit: str | None,
) -> tuple[Path, Path | None, str, str]:
    observed_system = normalize_system()
    if observed_system != profile.system:
        raise ExternalQualificationError(
            f"runner operating system is {observed_system!r}, not the claimed {profile.system!r} profile"
        )

    machine_id = os.environ.get("COMMUNITYAI_QUALIFICATION_MACHINE_ID", "")
    if not _MACHINE_ID_RE.fullmatch(machine_id):
        raise ExternalQualificationError("COMMUNITYAI_QUALIFICATION_MACHINE_ID must be a privacy-safe opaque label")

    artifact_value = os.environ.get(candidate.artifact_root_environment, "")
    artifact_root = Path(artifact_value).expanduser()
    if not artifact_value or not artifact_root.is_absolute() or not artifact_root.is_dir():
        raise ExternalQualificationError(
            f"{candidate.artifact_root_environment} must name an existing absolute snapshot directory"
        )

    cache_value = os.environ.get(candidate.cache_dir_environment, "")
    cache_dir = Path(cache_value).expanduser() if cache_value else None
    if cache_dir is not None and (not cache_dir.is_absolute() or not cache_dir.is_dir()):
        raise ExternalQualificationError(
            f"{candidate.cache_dir_environment} must name an existing absolute cache directory"
        )

    resolved_commit = source_commit or os.environ.get("GITHUB_SHA", "")
    if not _SOURCE_COMMIT_RE.fullmatch(resolved_commit):
        raise ExternalQualificationError("--source-commit or GITHUB_SHA must be an exact lowercase commit")
    if infer_source_commit() != resolved_commit:
        raise ExternalQualificationError("runner checkout does not match the claimed source commit")

    if not candidate.manifest.is_file():
        raise ExternalQualificationError(f"candidate manifest is missing: {candidate.manifest.name}")
    return artifact_root, cache_dir, machine_id, resolved_commit


def require_device(profile: HostProfile) -> None:
    if profile.device == "cpu":
        return
    import torch

    if profile.device == "cuda" and not torch.cuda.is_available():
        raise ExternalQualificationError("the CUDA-labelled runner has no available CUDA device")
    if profile.device == "mps":
        mps = getattr(torch.backends, "mps", None)
        if mps is None or not mps.is_available():
            raise ExternalQualificationError("the MPS-labelled runner has no available MPS device")


def preflight_candidate_snapshot(candidate: Candidate, artifact_root: Path) -> dict[str, int | str | bool]:
    try:
        manifest = ModelManifest.load(candidate.manifest)
        manifest.validate_artifact_layout(artifact_root)
    except (ManifestError, OSError):
        raise ExternalQualificationError("candidate snapshot layout does not match the exact manifest") from None
    return {
        "manifest_digest": manifest.digest_id,
        "artifact_layout_verified": True,
        "artifact_count": len(manifest.artifacts),
        "declared_artifact_bytes": sum(artifact.size for artifact in manifest.artifacts),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output is None and not args.preflight_only:
        raise ExternalQualificationError("--output is required unless --preflight-only is used")

    candidate = CANDIDATES[args.candidate]
    profile = HOST_PROFILES[args.profile]
    artifact_root, cache_dir, machine_id, source_commit = require_environment(
        candidate,
        profile,
        source_commit=args.source_commit,
    )
    require_device(profile)

    if args.preflight_only:
        snapshot = preflight_candidate_snapshot(candidate, artifact_root)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": "qualification-host-readiness",
                    "candidate": args.candidate,
                    "profile": args.profile,
                    "machine_id": machine_id,
                    "system": profile.system,
                    "device": profile.device,
                    "source_commit": source_commit,
                    **snapshot,
                    "result": "passed",
                    "qualification_evidence": False,
                    "complete_release_qualification": False,
                },
                sort_keys=True,
            )
        )
        return 0

    qualifier_args = [
        str(candidate.manifest),
        "--artifact-root",
        str(artifact_root),
        "--device",
        profile.device,
        "--with-failover",
        "--machine-id",
        machine_id,
        "--source-commit",
        source_commit,
        "--timeout",
        str(args.timeout),
        "--output",
        str(args.output),
    ]
    if cache_dir is not None:
        qualifier_args[3:3] = ["--cache-dir", str(cache_dir)]
    return qualify_main(qualifier_args)


if __name__ == "__main__":
    raise SystemExit(main())
