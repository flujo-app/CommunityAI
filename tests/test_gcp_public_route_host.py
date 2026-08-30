import json
import subprocess

import pytest

from scripts import gcp_public_route_host as host, public_route_acceptance as acceptance

RUN_ID = "route-20260829-a"
INITIAL_PEER = "/ip4/34.42.181.232/tcp/31337/p2p/QmYwAPJzv5CZsnAzt8auVZRnGi2Cj8Xn4K6q5V9z8M2w7P"
PRIMARY = "ghcr.io/flujo-app/communityai-public-route-qwen3.5-2b@sha256:" + "a" * 64
STANDBY = "ghcr.io/flujo-app/communityai-public-route-gemma-4-e2b@sha256:" + "b" * 64


def _completed(stdout=b"", returncode=0, stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_host_rejects_nonfixed_action_without_running_commands():
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return _completed()

    with pytest.raises(host.HostError, match="fixed action set"):
        host.execute_action(action="shell", run_id=RUN_ID, runner=runner)

    assert calls == []


def test_start_rejects_qualification_image_before_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(host, "STATE_ROOT", tmp_path)
    calls = []

    with pytest.raises(host.HostError, match="immutable CUDA"):
        host.execute_action(
            action="start-primary",
            run_id=RUN_ID,
            primary_image="ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b@sha256:" + "c" * 64,
            public_ipv4="34.42.181.232",
            initial_peer=INITIAL_PEER,
            runner=lambda argv, timeout: calls.append(tuple(argv)) or _completed(),
        )

    assert calls == []


@pytest.mark.parametrize(
    "extra,message",
    [
        ({"action_timeout_seconds": 3570}, "digest"),
        ({"acceptance_digest": "sha256:" + "d" * 64}, "timeout"),
        (
            {
                "acceptance_digest": "sha256:" + "d" * 64,
                "action_timeout_seconds": 119,
            },
            "timeout",
        ),
    ],
)
def test_start_requires_digest_and_bounded_action_timeout_before_docker(monkeypatch, tmp_path, extra, message):
    monkeypatch.setattr(host, "STATE_ROOT", tmp_path)
    calls = []

    with pytest.raises(host.HostError, match=message):
        host.execute_action(
            action="start-primary",
            run_id=RUN_ID,
            primary_image=PRIMARY,
            public_ipv4="34.42.181.232",
            initial_peer=INITIAL_PEER,
            runner=lambda argv, timeout: calls.append(tuple(argv)) or _completed(),
            **extra,
        )

    assert calls == []


def test_start_builds_one_shell_free_bounded_docker_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(host, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(host.os, "chown", lambda *_args: None, raising=False)
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        if argv[:3] == ("docker", "run", "--detach"):
            return _completed(b"container-id\n")
        return _completed()

    report = host.execute_action(
        action="start-primary",
        run_id=RUN_ID,
        primary_image=PRIMARY,
        public_ipv4="34.42.181.232",
        initial_peer=INITIAL_PEER,
        acceptance_digest="sha256:" + "d" * 64,
        action_timeout_seconds=3570,
        runner=runner,
    )

    assert report["details"] == {"candidate": "qwen3.5-2b", "started": True}
    assert calls[0] == (("docker", "pull", "--quiet", PRIMARY), 3570)
    assert calls[1][1] == 300
    docker_run = calls[1][0]
    assert docker_run[:3] == ("docker", "run", "--detach")
    assert "--read-only" in docker_run
    assert ("--security-opt", "no-new-privileges") == docker_run[
        docker_run.index("--security-opt") : docker_run.index("--security-opt") + 2
    ]
    assert "COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE=qwen3.5-2b" in docker_run
    assert all("\n" not in value and "\r" not in value for value in docker_run)
    state = json.loads((tmp_path / RUN_ID / "state.json").read_text())
    assert state["private"]["acceptance_digest"] == "sha256:" + "d" * 64
    assert state["private"]["initial_peer"] == INITIAL_PEER


def test_bootstrap_preflight_requires_exact_pinned_readiness(monkeypatch, tmp_path):
    readiness = tmp_path / "runtime-ready.json"
    readiness.write_text(
        json.dumps(
            {
                "container_runtime": "docker",
                "containerd_version": "2.2.1-0ubuntu1~24.04.3",
                "docker_version": "29.1.3-0ubuntu3~24.04.2",
                "gpu_driver_version": "570.211.01",
                "nvidia_container_toolkit_version": "1.20.0-1",
                "ready": True,
                "schema_version": 1,
                "scope": "communityai-public-route-bootstrap",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(host, "BOOTSTRAP_READY", readiness)

    def runner(argv, timeout):
        if tuple(argv) == ("docker", "info", "--format", "{{.DefaultRuntime}}"):
            return _completed(b"nvidia\n")
        if argv[0] == "nvidia-smi":
            return _completed(b"570.211.01\n")
        return _completed()

    report = host.execute_action(action="preflight", run_id=RUN_ID, runner=runner)

    assert report["details"] == {
        "bootstrap_ready": True,
        "docker_ready": True,
        "gpu_ready": True,
    }

    def wrong_driver(argv, timeout):
        if tuple(argv) == (
            "docker",
            "info",
            "--format",
            "{{.DefaultRuntime}}",
        ):
            return _completed(b"nvidia\n")
        if argv[0] == "nvidia-smi":
            return _completed(b"999.0\n")
        return _completed()

    with pytest.raises(host.HostError, match="live GPU driver"):
        host.execute_action(action="preflight", run_id=RUN_ID, runner=wrong_driver)

    changed = json.loads(readiness.read_text())
    changed["docker_version"] = "latest"
    readiness.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(host.HostError, match="pinned runtime"):
        host.execute_action(action="preflight", run_id=RUN_ID, runner=runner)


def test_host_acknowledgement_is_bounded_and_marker_framed():
    report = {
        "schema_version": 1,
        "scope": "gcp-public-route-host-action",
        "result": "passed",
        "action": "preflight",
        "details": {"bootstrap_ready": True},
    }

    framed = host._encode_acknowledgement(report)

    assert framed.startswith(host.ACK_PREFIX)
    assert json.loads(framed[len(host.ACK_PREFIX) :]) == report


def test_log_accounting_rejects_missing_relative_or_nonregular_paths(tmp_path):
    log = tmp_path / "container.log"
    log.write_bytes(b"1234")

    assert host._container_log_bytes(str(log).encode()) == 4
    for payload in (b"", b"relative.log", str(tmp_path).encode()):
        with pytest.raises(host.HostError, match="log accounting"):
            host._container_log_bytes(payload)


def test_host_command_boundary_rejects_newlines_before_subprocess():
    with pytest.raises(host.CommandError, match="contract"):
        host._run_bounded(("docker", "pull\nmalicious"), 30)


@pytest.mark.parametrize("value", ["0", "901", "not-an-int"])
def test_acceptance_timeout_is_bounded(value):
    with pytest.raises(Exception):
        acceptance._bounded_timeout(value)


def test_acceptance_peer_and_candidate_validation_fail_before_model_loading():
    with pytest.raises(acceptance.AcceptanceError, match="candidate"):
        acceptance.run_probe(candidate="unknown", initial_peer=INITIAL_PEER, timeout=1)
    with pytest.raises(acceptance.AcceptanceError, match="bootstrap peer"):
        acceptance.run_probe(candidate="qwen3.5-2b", initial_peer="/ip4/127.0.0.1/tcp/1", timeout=1)
