import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import (
    gcp_public_route_cache_lifecycle as cache,
    gcp_public_route_lifecycle as route,
    qualification_cost_guard as guard,
)

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "gcp_public_route_cache_startup.sh"
QWEN = ROOT / "docs" / "evidence" / "gate11pub-20260829-a-qwen3.5-2b-publication-evidence.json"
GEMMA = ROOT / "docs" / "evidence" / "gate11pub-20260829-a-gemma-4-e2b-publication-evidence.json"
SOURCE_COMMIT = "a" * 40


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path):
    primary = json.loads(QWEN.read_text(encoding="utf-8"))
    standby = json.loads(GEMMA.read_text(encoding="utf-8"))
    values = {
        "entries": (),
        "run_id": "cache-20260830-a",
        "provider": "gcp",
        "workload": guard.GCP_PUBLIC_ROUTE_CACHE_WORKLOAD,
        "purpose": "Gate 11 private same-region route image cache",
        "source_commit": SOURCE_COMMIT,
        "maximum_hours": Decimal("6"),
        "project": guard.GCP_ARTIFACT_REGISTRY_PROJECT,
        "zone": "us-central1-a",
        "windows_image": None,
        "linux_image": "ubuntu-2404-noble-amd64-v20260826",
        "cuda_fallback_zone": None,
        "cuda_shape": "n1-t4",
        "manual_maximum_usd": None,
        "primary_image": primary["image_reference"],
        "primary_image_evidence_digest": _digest(QWEN),
        "standby_image": standby["image_reference"],
        "standby_image_evidence_digest": _digest(GEMMA),
        "cache_bootstrap_digest": _digest(BOOTSTRAP),
        "cache_bootstrap_bytes": BOOTSTRAP.stat().st_size,
        "today": date(2026, 8, 29),
    }
    planned = guard.build_authorization(**values)
    reservation = guard.LedgerEntry(
        run_id=planned["run_id"],
        provider="GCP",
        purpose=planned["ledger_purpose"],
        maximum_usd=Decimal("10"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )
    values["entries"] = (reservation,)
    authorization = guard.build_authorization(**values)
    authorization_path = tmp_path / "authorization.json"
    _write_json(authorization_path, authorization)
    ledger_path = tmp_path / "ledger.md"
    ledger_path.write_text(
        "# Readiness\n\n"
        "## Cloud authorization and spend ledger\n\n"
        "| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |\n"
        "| --- | --- | --- | ---: | ---: | --- | --- |\n"
        + authorization["required_ledger_row"]
        + "\n\nRemaining authorized maximum: **USD 90**.\n",
        encoding="utf-8",
    )
    inputs = {
        "authorization_path": authorization_path,
        "ledger_path": ledger_path,
        "primary_evidence_path": QWEN,
        "standby_evidence_path": GEMMA,
        "cache_bootstrap_path": BOOTSTRAP,
        "expected_source_commit": SOURCE_COMMIT,
    }
    return cache.load_bound_cache_plan(**inputs), inputs


def _repo_json(plan):
    return json.dumps(
        {
            "name": (
                f"projects/{plan.project}/locations/{plan.region}/"
                f"repositories/{guard.GCP_ARTIFACT_REGISTRY_REPOSITORY}"
            ),
            "format": "DOCKER",
            "mode": "REMOTE_REPOSITORY",
            "remoteRepositoryConfig": {"commonRepository": {"uri": "https://ghcr.io"}},
            "vulnerabilityScanningConfig": {
                "enablementConfig": "DISABLED",
                "enablementState": "SCANNING_DISABLED",
            },
        }
    ).encode()


@pytest.mark.parametrize(
    "remote_config",
    [
        {"dockerRepository": {"customRepository": {"uri": "https://ghcr.io"}}},
        {"commonRepository": {"uri": "https://ghcr.io/"}},
    ],
)
def test_repository_exact_rejects_non_provider_upstream_shape(tmp_path, remote_config):
    plan, _inputs = _fixture(tmp_path)
    payload = json.loads(_repo_json(plan))
    payload["remoteRepositoryConfig"] = remote_config

    def runner(_argv, _timeout):
        return route.CommandResult(0, json.dumps(payload).encode(), b"")

    assert cache._repository_exact(plan, runner) is False


def _ready():
    return json.dumps(
        {
            "schema_version": 1,
            "scope": "communityai-public-route-cache-bootstrap",
            "result": "passed",
            "images_prefetched": 2,
            "registry_credentials_removed": True,
        }
    ).encode()


def _failed_ready():
    return json.dumps(
        {
            "schema_version": 1,
            "scope": "communityai-public-route-cache-bootstrap",
            "result": "failed",
            "failure_code": "cache_bootstrap_failed",
            "registry_credentials_removed": True,
        }
    ).encode()


def _github_public(argv):
    if argv[:4] == ("gh", "auth", "status", "--hostname"):
        return route.CommandResult(0, b"", b"")
    if argv[:2] == ("gh", "api"):
        return route.CommandResult(0, b"public\n", b"")
    return None


def _enabled_service(argv):
    service = (
        "iam.googleapis.com"
        if any("iam.googleapis.com" in part for part in argv)
        else "artifactregistry.googleapis.com"
    )
    return route.CommandResult(0, (service + "\n").encode(), b"")


def test_cache_bootstrap_uses_ephemeral_metadata_identity_without_public_access():
    body = BOOTSTRAP.read_text(encoding="utf-8")

    assert "instance/service-accounts/default" in body
    assert "ca-[0-9a-f]{20}@community-ai-506321" in body
    assert "--username oauth2accesstoken --password-stdin" in body
    assert 'docker --config "${REGISTRY_CONFIG}" pull' in body
    assert 'rm -rf -- "${REGISTRY_CONFIG}"' in body
    assert "cleanup_registry() {\n  access_token=''\n  token_payload=''" in body
    assert "  cleanup_registry\n  if [[ ${status} -ne 0 ]]; then" in body
    assert '"registry_credentials_removed":true' in body
    assert '"failure_code":"cache_bootstrap_failed"' in body
    assert "trap fail_closed EXIT" in body
    assert 'READY_TEMP="${READY_FILE}.tmp"' in body
    assert '>"${READY_TEMP}"' in body
    assert 'mv -f -- "${READY_TEMP}" "${READY_FILE}"' in body
    assert '>"${READY_FILE}"' not in body
    assert "allUsers" not in body
    assert "service-account-key" not in body
    assert body.index("--password-stdin") < body.index('pull_one "${primary_image}"')
    assert body.rindex("cleanup_registry") < body.rindex("registry_credentials_removed")


def test_cache_ready_probe_waits_for_atomic_nonempty_regular_file(tmp_path):
    plan, _inputs = _fixture(tmp_path)
    calls = []
    results = iter(
        [
            route.CommandResult(1, b"", b"not ready"),
            route.CommandResult(0, _ready(), b""),
        ]
    )

    def runner(argv, _timeout):
        calls.append(tuple(argv))
        return next(results)

    cache._wait_cache_ready(
        plan,
        runner,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert len(calls) == 2
    command = calls[0][calls[0].index("--command") + 1]
    assert command == (
        f"sudo -n test -f {cache.READY_PATH} && "
        f"sudo -n test ! -L {cache.READY_PATH} && "
        f"sudo -n test -s {cache.READY_PATH} && "
        f"sudo -n cat {cache.READY_PATH}"
    )


def test_cache_ready_probe_fails_immediately_on_atomic_failure_acknowledgement(tmp_path):
    plan, _inputs = _fixture(tmp_path)
    slept = []

    with pytest.raises(cache.CacheLifecycleError, match="bootstrap failed"):
        cache._wait_cache_ready(
            plan,
            lambda _argv, _timeout: route.CommandResult(0, _failed_ready(), b""),
            clock=lambda: 0.0,
            sleeper=slept.append,
        )

    assert slept == []


def test_bound_cache_plan_validates_ledger_publication_and_source(tmp_path):
    plan, _inputs = _fixture(tmp_path)

    assert plan.run_id == "cache-20260830-a"
    assert plan.primary.image_reference.startswith(guard.GCP_PRIMARY_IMAGE_REPOSITORY + "@")
    assert plan.standby.runtime_image_reference.startswith(guard.GCP_STANDBY_IMAGE_REPOSITORY + "@")
    assert plan.provider_plan["repository"]["private_after_prewarm"] is True


def test_bound_cache_plan_rejects_authorization_mutation(tmp_path):
    _plan, inputs = _fixture(tmp_path)
    value = json.loads(inputs["authorization_path"].read_text(encoding="utf-8"))
    value["provider_plan"]["repository"]["upstream"] = "https://example.invalid"
    _write_json(inputs["authorization_path"], value)

    with pytest.raises(cache.CacheLifecycleError, match="digest"):
        cache.load_bound_cache_plan(**inputs)


def test_private_policy_rejects_public_or_malformed_members(tmp_path):
    plan, _inputs = _fixture(tmp_path)

    def public_runner(_argv, _timeout):
        return route.CommandResult(
            0,
            b'{"bindings":[{"role":"roles/artifactregistry.reader","members":["allUsers"]}]}',
            b"",
        )

    assert cache._private_policy(plan, public_runner) is False

    def malformed_runner(_argv, _timeout):
        return route.CommandResult(0, b'{"bindings":[{"members":"allUsers"}]}', b"")

    with pytest.raises(cache.CacheLifecycleError, match="policy"):
        cache._private_policy(plan, malformed_runner)


def test_cache_lifecycle_never_cleans_unproven_targets_before_initial_absence(tmp_path):
    plan, _inputs = _fixture(tmp_path)
    calls = []

    def runner(argv, _timeout):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:4] == ("gcloud", "auth", "list", "--filter=status:ACTIVE"):
            return route.CommandResult(1, b"", b"")
        if argv[:4] == ("gcloud", "compute", "instances", "list") and any(
            "communityai-bootstrap-1" in part for part in argv
        ):
            return route.CommandResult(0, b"RUNNING\n", b"")
        raise AssertionError(argv)

    evidence = cache.execute_cache_lifecycle(
        plan,
        output_path=tmp_path / "evidence.json",
        runner=runner,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert evidence["result"] == "failed"
    assert evidence["failure_stage"] == "native_authentication"
    assert evidence["privacy"]["credentials_retained"] is False
    assert not any(tuple(command) in calls for command in plan.provider_plan["cleanup_commands"])


def test_cache_lifecycle_rejects_private_upstream_before_gcp_mutation(tmp_path):
    plan, _inputs = _fixture(tmp_path)
    calls = []

    def runner(argv, _timeout):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:4] == ("gcloud", "auth", "list", "--filter=status:ACTIVE"):
            return route.CommandResult(0, b"owner@example.com\n", b"")
        if argv[:4] == ("gh", "auth", "status", "--hostname"):
            return route.CommandResult(0, b"", b"")
        if argv[:2] == ("gh", "api"):
            return route.CommandResult(0, b"private\n", b"")
        raise AssertionError(argv)

    evidence = cache.execute_cache_lifecycle(
        plan,
        output_path=tmp_path / "evidence.json",
        runner=runner,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert evidence["result"] == "failed"
    assert evidence["failure_stage"] == "upstream_visibility"
    assert not any(argv[:3] == ("gcloud", "services", "enable") for argv in calls)
    assert not any(tuple(command) in calls for command in plan.provider_plan["cleanup_commands"])


def test_cache_lifecycle_passes_only_after_private_digest_verification_and_cleanup(tmp_path):
    plan, _inputs = _fixture(tmp_path)
    calls = []

    def runner(argv, _timeout):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:4] == ("gcloud", "auth", "list", "--filter=status:ACTIVE"):
            return route.CommandResult(0, b"owner@example.com\n", b"")
        github = _github_public(argv)
        if github is not None:
            return github
        if argv[:4] == ("gcloud", "services", "enable", "artifactregistry.googleapis.com"):
            return route.CommandResult(1, b"", b"provider response lost")
        if argv[:4] == ("gcloud", "services", "list", "--enabled"):
            return _enabled_service(argv)
        if argv[:4] == ("gcloud", "compute", "instances", "list") and any(
            "communityai-bootstrap-1" in part for part in argv
        ):
            return route.CommandResult(0, b"RUNNING\n", b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "list"):
            return route.CommandResult(0, b"", b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "describe"):
            return route.CommandResult(0, _repo_json(plan), b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "get-iam-policy"):
            return route.CommandResult(0, b'{"bindings":[]}', b"")
        if argv[:5] == ("gcloud", "artifacts", "docker", "images", "describe"):
            return route.CommandResult(0, (argv[5].rsplit("@", 1)[1] + "\n").encode(), b"")
        if argv[:4] == ("gcloud", "compute", "instances", "describe"):
            return route.CommandResult(
                0,
                json.dumps(
                    {
                        "status": "RUNNING",
                        "machineType": "projects/p/zones/z/machineTypes/e2-standard-4",
                        "scheduling": {"maxRunDuration": {"seconds": "21600", "nanos": 0}},
                        "serviceAccounts": [
                            {
                                "email": plan.provider_plan["builder"]["service_account"],
                                "scopes": plan.provider_plan["builder"]["scopes"],
                            }
                        ],
                    }
                ).encode(),
                b"",
            )
        if argv[:4] == ("gcloud", "compute", "ssh", plan.provider_plan["builder"]["instance"]):
            return route.CommandResult(0, _ready(), b"")
        return route.CommandResult(0, b"", b"")

    evidence = cache.execute_cache_lifecycle(
        plan,
        output_path=tmp_path / "evidence.json",
        runner=runner,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert evidence["result"] == "passed"
    assert evidence["cached_manifest_count"] == 4
    assert evidence["repository"] == {
        "retained": True,
        "private": True,
        "mode": "REMOTE_REPOSITORY",
        "location": "us-central1",
        "upstream": "https://ghcr.io",
        "absent_after_failure": None,
    }
    assert evidence["temporary_public_access_used"] is False
    assert evidence["builder_identity"]["removed"] is True
    assert evidence["builder_cleanup"]["all_absent"] is True
    assert not any("delete" in argv and guard.GCP_ARTIFACT_REGISTRY_REPOSITORY in argv for argv in calls)


def test_cache_lifecycle_failure_removes_new_repository_and_builder(tmp_path):
    plan, _inputs = _fixture(tmp_path)

    def runner(argv, _timeout):
        argv = tuple(argv)
        if argv[:4] == ("gcloud", "auth", "list", "--filter=status:ACTIVE"):
            return route.CommandResult(0, b"owner@example.com\n", b"")
        github = _github_public(argv)
        if github is not None:
            return github
        if argv[:4] == ("gcloud", "services", "list", "--enabled"):
            return _enabled_service(argv)
        if argv[:4] == ("gcloud", "compute", "instances", "list") and any(
            "communityai-bootstrap-1" in part for part in argv
        ):
            return route.CommandResult(0, b"RUNNING\n", b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "list"):
            return route.CommandResult(0, b"", b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "describe"):
            return route.CommandResult(0, _repo_json(plan), b"")
        if argv[:4] == ("gcloud", "compute", "instances", "describe"):
            return route.CommandResult(
                0,
                json.dumps(
                    {
                        "status": "RUNNING",
                        "machineType": "projects/p/zones/z/machineTypes/e2-standard-4",
                        "scheduling": {"maxRunDuration": {"seconds": "21600", "nanos": 0}},
                        "serviceAccounts": [
                            {
                                "email": plan.provider_plan["builder"]["service_account"],
                                "scopes": plan.provider_plan["builder"]["scopes"],
                            }
                        ],
                    }
                ).encode(),
                b"",
            )
        if argv[:4] == ("gcloud", "compute", "ssh", plan.provider_plan["builder"]["instance"]):
            return route.CommandResult(0, b'{"result":"wrong"}', b"")
        return route.CommandResult(0, b"", b"")

    evidence = cache.execute_cache_lifecycle(
        plan,
        output_path=tmp_path / "evidence.json",
        runner=runner,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert evidence["result"] == "failed"
    assert evidence["failure_stage"] == "cache_warm"
    assert evidence["repository"]["retained"] is False
    assert evidence["repository"]["absent_after_failure"] is True
    assert evidence["temporary_public_access_used"] is False
    assert evidence["builder_identity"]["removed"] is True
    assert evidence["builder_cleanup"]["all_absent"] is True
    assert evidence["protected_bootstrap_running"] is True


def test_cache_lifecycle_cleans_repository_when_create_applies_but_reports_failure(tmp_path):
    plan, _inputs = _fixture(tmp_path)
    state = {"repository_exists": False}
    calls = []
    delete_repository = tuple(plan.provider_plan["delete_repository_command"])

    def runner(argv, _timeout):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:4] == ("gcloud", "auth", "list", "--filter=status:ACTIVE"):
            return route.CommandResult(0, b"owner@example.com\n", b"")
        github = _github_public(argv)
        if github is not None:
            return github
        if argv[:4] == ("gcloud", "services", "list", "--enabled"):
            return _enabled_service(argv)
        if argv[:4] == ("gcloud", "compute", "instances", "list") and any(
            "communityai-bootstrap-1" in part for part in argv
        ):
            return route.CommandResult(0, b"RUNNING\n", b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "list"):
            output = b"communityai-ghcr-cache\n" if state["repository_exists"] else b""
            return route.CommandResult(0, output, b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "create"):
            state["repository_exists"] = True
            return route.CommandResult(1, b"", b"provider response lost")
        if argv == delete_repository:
            state["repository_exists"] = False
            return route.CommandResult(1, b"", b"provider response lost")
        return route.CommandResult(0, b"", b"")

    evidence = cache.execute_cache_lifecycle(
        plan,
        output_path=tmp_path / "evidence.json",
        runner=runner,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert evidence["result"] == "failed"
    assert evidence["failure_stage"] == "repository_create"
    assert delete_repository in calls
    assert state["repository_exists"] is False
    assert evidence["repository"]["absent_after_failure"] is True
    assert evidence["temporary_public_access_used"] is False
    assert evidence["builder_identity"]["removed"] is True


def test_cache_lifecycle_revokes_identity_when_binding_applies_but_reports_failure(tmp_path):
    plan, _inputs = _fixture(tmp_path)
    state = {"repository_exists": False, "reader_binding": False}
    calls = []
    create_repository = tuple(plan.provider_plan["create_commands"][2])
    add_binding = tuple(plan.provider_plan["create_commands"][4])
    revoke_binding = tuple(plan.provider_plan["revoke_builder_reader_command"])
    delete_repository = tuple(plan.provider_plan["delete_repository_command"])

    def runner(argv, _timeout):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:4] == ("gcloud", "auth", "list", "--filter=status:ACTIVE"):
            return route.CommandResult(0, b"owner@example.com\n", b"")
        github = _github_public(argv)
        if github is not None:
            return github
        if argv[:4] == ("gcloud", "services", "list", "--enabled"):
            return _enabled_service(argv)
        if argv[:4] == ("gcloud", "compute", "instances", "list") and any(
            "communityai-bootstrap-1" in part for part in argv
        ):
            return route.CommandResult(0, b"RUNNING\n", b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "list"):
            output = b"communityai-ghcr-cache\n" if state["repository_exists"] else b""
            return route.CommandResult(0, output, b"")
        if argv == create_repository:
            state["repository_exists"] = True
            return route.CommandResult(0, b"", b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "describe"):
            return route.CommandResult(0, _repo_json(plan), b"")
        if argv == add_binding:
            state["reader_binding"] = True
            return route.CommandResult(1, b"", b"provider response lost")
        if argv == revoke_binding:
            state["reader_binding"] = False
            return route.CommandResult(0, b"", b"")
        if argv == delete_repository:
            state["repository_exists"] = False
            return route.CommandResult(0, b"", b"")
        return route.CommandResult(0, b"", b"")

    evidence = cache.execute_cache_lifecycle(
        plan,
        output_path=tmp_path / "evidence.json",
        runner=runner,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert evidence["result"] == "failed"
    assert evidence["failure_stage"] == "builder_identity"
    assert revoke_binding in calls
    assert state == {"repository_exists": False, "reader_binding": False}
    assert evidence["repository"]["absent_after_failure"] is True
    assert evidence["temporary_public_access_used"] is False
    assert evidence["builder_identity"]["removed"] is True
