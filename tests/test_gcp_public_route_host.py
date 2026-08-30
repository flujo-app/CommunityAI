import base64
import json
import subprocess
import threading

import pytest

from scripts import gcp_public_route_host as host, public_route_acceptance as acceptance

RUN_ID = "route-20260829-a"
INITIAL_PEER = "/ip4/34.42.181.232/tcp/31337/p2p/QmYwAPJzv5CZsnAzt8auVZRnGi2Cj8Xn4K6q5V9z8M2w7P"
PRIMARY = "ghcr.io/flujo-app/communityai-public-route-qwen3.5-2b@sha256:" + "a" * 64
STANDBY = "ghcr.io/flujo-app/communityai-public-route-gemma-4-e2b@sha256:" + "b" * 64


def _completed(stdout=b"", returncode=0, stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.skipif(host.os.name != "posix", reason="Linux upload ownership contract")
def test_registry_upload_is_owner_only_consumed_once_and_cleanup_is_idempotent(
    monkeypatch,
    tmp_path,
):
    upload_id = "a" * 32
    monkeypatch.setattr(host, "REGISTRY_UPLOAD_PARENT", tmp_path)
    monkeypatch.setattr(host, "_fsync_upload_parent", lambda: None)
    monkeypatch.setattr(host, "_sudo_identity", lambda: (host.os.getuid(), host.os.getgid()))

    assert host._prepare_registry_upload(RUN_ID, upload_id) == {"registry_upload_ready": True}
    root, payload = host._registry_upload_paths(RUN_ID, upload_id)
    payload.write_bytes(b"bmF0aXZlLWdoLXRva2Vu\n")
    payload.chmod(0o644)

    consumed = host._read_registry_upload(RUN_ID, upload_id)

    assert bytes(consumed) == b"bmF0aXZlLWdoLXRva2Vu\n"
    assert not root.exists()
    assert host._remove_registry_upload(RUN_ID, upload_id) is True
    consumed[:] = b"\x00" * len(consumed)
    assert set(consumed) <= {0}


@pytest.mark.skipif(host.os.name != "posix", reason="Linux upload ownership contract")
def test_registry_upload_rejects_hardlinks_and_removes_staging(monkeypatch, tmp_path):
    upload_id = "b" * 32
    monkeypatch.setattr(host, "REGISTRY_UPLOAD_PARENT", tmp_path)
    monkeypatch.setattr(host, "_fsync_upload_parent", lambda: None)
    monkeypatch.setattr(host, "_sudo_identity", lambda: (host.os.getuid(), host.os.getgid()))

    host._prepare_registry_upload(RUN_ID, upload_id)
    root, payload = host._registry_upload_paths(RUN_ID, upload_id)
    payload.write_bytes(b"bmF0aXZlLWdoLXRva2Vu\n")
    host.os.link(payload, root / "credential-link.b64")

    with pytest.raises(host.HostError, match="file contract"):
        host._read_registry_upload(RUN_ID, upload_id)

    assert not root.exists()


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
        if argv[:3] == ("docker", "image", "inspect"):
            return _completed(json.dumps([PRIMARY]).encode())
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
    assert calls[0] == (("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", PRIMARY), 60)
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


def test_start_classifies_post_pull_command_failure_without_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(host, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(host.os, "chown", lambda *_args: None, raising=False)
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        if argv[:3] == ("docker", "image", "inspect"):
            return _completed(json.dumps([PRIMARY]).encode())
        if argv[:3] == ("docker", "run", "--detach"):
            raise host.CommandError("container start failed")
        return _completed()

    with pytest.raises(host.ActionFailure) as failure:
        host.execute_action(
            action="start-primary",
            run_id=RUN_ID,
            primary_image=PRIMARY,
            public_ipv4="34.42.181.232",
            initial_peer=INITIAL_PEER,
            acceptance_digest="sha256:" + "d" * 64,
            action_timeout_seconds=3570,
            runner=runner,
        )

    assert failure.value.failure_code == "host_command"
    assert [call[0][:3] for call in calls] == [
        ("docker", "image", "inspect"),
        ("docker", "run", "--detach"),
    ]


def test_bootstrap_preflight_requires_exact_pinned_readiness(monkeypatch, tmp_path):
    readiness = tmp_path / "runtime-ready.json"
    daemon_config = tmp_path / "daemon.json"
    daemon_config.write_text(json.dumps({"max-concurrent-downloads": 1}), encoding="utf-8")
    readiness.write_text(
        json.dumps(
            {
                "container_runtime": "docker",
                "containerd_version": "2.2.1-0ubuntu1~24.04.3",
                "docker_max_concurrent_downloads": 1,
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
    monkeypatch.setattr(host, "DOCKER_DAEMON_CONFIG", daemon_config)

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

    for invalid_config in ({}, {"max-concurrent-downloads": 2}, {"max-concurrent-downloads": True}):
        daemon_config.write_text(json.dumps(invalid_config), encoding="utf-8")
        with pytest.raises(host.HostError, match="download concurrency"):
            host.execute_action(action="preflight", run_id=RUN_ID, runner=runner)
    daemon_config.write_text(json.dumps({"max-concurrent-downloads": 1}), encoding="utf-8")

    boolean_readiness = json.loads(readiness.read_text())
    boolean_readiness["docker_max_concurrent_downloads"] = True
    readiness.write_text(json.dumps(boolean_readiness), encoding="utf-8")
    with pytest.raises(host.HostError, match="pinned runtime"):
        host.execute_action(action="preflight", run_id=RUN_ID, runner=runner)
    boolean_readiness["docker_max_concurrent_downloads"] = 1
    readiness.write_text(json.dumps(boolean_readiness), encoding="utf-8")

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


def test_action_failure_acknowledgement_contains_only_allowlisted_code():
    report = host._action_failure_report("start-primary", "image_pull")
    framed = host._encode_acknowledgement(report)

    assert json.loads(framed[len(host.ACK_PREFIX) :]) == {
        "schema_version": 1,
        "scope": "gcp-public-route-host-action",
        "result": "failed",
        "action": "start-primary",
        "details": {"failure_code": "image_pull"},
    }
    with pytest.raises(host.HostError, match="acknowledgement"):
        host._action_failure_report("start-primary", "provider_stderr")


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


def test_immutable_pull_retries_transient_command_failures_within_deadline():
    now = [0.0]
    calls = []
    sleeps = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        if len(calls) < 3:
            raise host.CommandError("transient pull failure")
        return _completed()

    def sleeper(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    host._pull_immutable_image(
        image=PRIMARY,
        deadline=100.0,
        maximum_command_timeout_seconds=100,
        runner=runner,
        clock=lambda: now[0],
        sleeper=sleeper,
    )

    assert [timeout for _argv, timeout in calls] == [100, 95, 80]
    assert all(argv == ("docker", "pull", "--quiet", PRIMARY) for argv, _timeout in calls)
    assert sleeps == [5.0, 15.0]


def test_immutable_pull_uses_final_bounded_backoff_before_succeeding():
    now = [0.0]
    calls = []
    sleeps = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        if len(calls) < 5:
            raise host.CommandError("transient pull failure")
        return _completed()

    def sleeper(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    host._pull_immutable_image(
        image=PRIMARY,
        deadline=3570.0,
        maximum_command_timeout_seconds=3570,
        runner=runner,
        clock=lambda: now[0],
        sleeper=sleeper,
    )

    assert [timeout for _argv, timeout in calls] == [3570, 3565, 3550, 3490, 3370]
    assert all(argv == ("docker", "pull", "--quiet", PRIMARY) for argv, _timeout in calls)
    assert sleeps == [5.0, 15.0, 60.0, 120.0]


def test_immutable_pull_retains_classification_after_all_bounded_attempts():
    now = [0.0]
    calls = []
    sleeps = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        raise host.CommandError("persistent pull failure")

    def sleeper(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    with pytest.raises(host.ActionFailure) as failure:
        host._pull_immutable_image(
            image=PRIMARY,
            deadline=3570.0,
            maximum_command_timeout_seconds=3570,
            runner=runner,
            clock=lambda: now[0],
            sleeper=sleeper,
        )

    assert failure.value.failure_code == "image_pull"
    assert [timeout for _argv, timeout in calls] == [3570, 3565, 3550, 3490, 3370]
    assert all(argv == ("docker", "pull", "--quiet", PRIMARY) for argv, _timeout in calls)
    assert sleeps == [5.0, 15.0, 60.0, 120.0]


def test_immutable_pull_clamps_float_rounding_to_action_timeout():
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        return _completed()

    host._pull_immutable_image(
        image=PRIMARY,
        deadline=3570.0000000000005,
        maximum_command_timeout_seconds=3570,
        runner=runner,
        clock=lambda: 0.0,
    )

    assert calls == [(("docker", "pull", "--quiet", PRIMARY), 3570)]


def test_immutable_pull_fails_closed_when_retry_delay_exhausts_deadline():
    now = [0.0]
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        raise host.CommandError("persistent pull failure")

    with pytest.raises(host.ActionFailure) as failure:
        host._pull_immutable_image(
            image=PRIMARY,
            deadline=10.0,
            maximum_command_timeout_seconds=10,
            runner=runner,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

    assert failure.value.failure_code == "image_pull"
    assert [timeout for _argv, timeout in calls] == [10, 5]


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"dG9rZW4=\r\n",
        b"dG9rZW4=\nextra\n",
        b"\xef\xbb\xbfdG9rZW4=\n",
        b"dG9r\x00ZW4=\n",
        b"not-base64!\n",
    ],
)
def test_registry_secret_decoder_rejects_ambiguous_bytes(payload):
    with pytest.raises(host.HostError):
        host._strict_base64_secret(payload, "test secret")


def test_transport_sentinel_requires_exact_canonical_bytes():
    payload = base64.b64encode(host.TRANSPORT_SENTINEL) + b"\n"

    assert host._transport_sentinel(payload) == {"transport_verified": True}

    with pytest.raises(host.HostError, match="changed"):
        host._transport_sentinel(base64.b64encode(b"wrong") + b"\n")


def test_authenticated_prefetch_pulls_both_digests_and_removes_credentials(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    monkeypatch.setattr(host, "REGISTRY_CONFIG", registry)
    monkeypatch.setattr(host, "_prepare_registry_config", lambda: registry.mkdir())
    calls = []
    secrets = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        if argv[:3] == ("docker", "image", "inspect"):
            return _completed(json.dumps([argv[-1]]).encode())
        return _completed()

    def secret_runner(argv, timeout, secret):
        secrets.append((tuple(argv), timeout, bytes(secret)))
        return 0

    report = host.execute_action(
        action="prefetch-images-file",
        run_id=RUN_ID,
        primary_image=PRIMARY,
        standby_image=STANDBY,
        registry_user="octocat",
        secret_payload=base64.b64encode(b"native-gh-token") + b"\n",
        action_timeout_seconds=3570,
        runner=runner,
        secret_runner=secret_runner,
    )

    assert report["details"] == {"images_prefetched": 2, "registry_credentials_removed": True}
    assert secrets[0][0] == (
        "docker",
        "--config",
        str(registry),
        "login",
        "ghcr.io",
        "--username",
        "octocat",
        "--password-stdin",
    )
    assert secrets[0][2] == b"native-gh-token\n"
    assert all("native-gh-token" not in value for value in secrets[0][0])
    pull_argv = [argv for argv, _timeout in calls if "pull" in argv]
    assert set(pull_argv) == {
        ("docker", "--config", str(registry), "pull", "--quiet", PRIMARY),
        ("docker", "--config", str(registry), "pull", "--quiet", STANDBY),
    }
    assert not registry.exists()


def test_authenticated_prefetch_pulls_images_concurrently(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    monkeypatch.setattr(host, "REGISTRY_CONFIG", registry)
    monkeypatch.setattr(host, "_prepare_registry_config", lambda: registry.mkdir())
    rendezvous = threading.Barrier(2)
    pulled = []

    def pull(**kwargs):
        pulled.append(kwargs["image"])
        rendezvous.wait(timeout=2)

    def runner(argv, _timeout):
        if argv[:3] == ("docker", "image", "inspect"):
            return _completed(json.dumps([argv[-1]]).encode())
        return _completed()

    monkeypatch.setattr(host, "_pull_immutable_image", pull)

    report = host.execute_action(
        action="prefetch-images-file",
        run_id=RUN_ID,
        primary_image=PRIMARY,
        standby_image=STANDBY,
        registry_user="octocat",
        secret_payload=base64.b64encode(b"native-gh-token") + b"\n",
        action_timeout_seconds=3570,
        runner=runner,
        secret_runner=lambda _argv, _timeout, _secret: 0,
    )

    assert set(pulled) == {PRIMARY, STANDBY}
    assert report["details"] == {"images_prefetched": 2, "registry_credentials_removed": True}
    assert not registry.exists()


def test_authenticated_prefetch_cleans_registry_on_pull_exception(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    monkeypatch.setattr(host, "REGISTRY_CONFIG", registry)
    monkeypatch.setattr(host, "_prepare_registry_config", lambda: registry.mkdir())
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        return _completed()

    monkeypatch.setattr(
        host,
        "_pull_immutable_image",
        lambda **_kwargs: (_ for _ in ()).throw(host.ActionFailure("image_pull")),
    )

    with pytest.raises(host.ActionFailure) as failure:
        host.execute_action(
            action="prefetch-images-file",
            run_id=RUN_ID,
            primary_image=PRIMARY,
            standby_image=STANDBY,
            registry_user="octocat",
            secret_payload=base64.b64encode(b"native-gh-token") + b"\n",
            action_timeout_seconds=3570,
            runner=runner,
            secret_runner=lambda _argv, _timeout, _secret: 0,
        )

    assert failure.value.failure_code == "image_pull"
    assert any(argv[-2:] == ("logout", "ghcr.io") for argv in calls)
    assert not registry.exists()


def test_registry_prefetch_rejects_and_removes_preexisting_nondirectory(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    registry.write_text("unsafe", encoding="utf-8")
    monkeypatch.setattr(host, "REGISTRY_CONFIG", registry)

    with pytest.raises(host.HostError, match="already exists"):
        host.execute_action(
            action="prefetch-images-file",
            run_id=RUN_ID,
            primary_image=PRIMARY,
            standby_image=STANDBY,
            registry_user="octocat",
            secret_payload=base64.b64encode(b"native-gh-token") + b"\n",
            action_timeout_seconds=3570,
            runner=lambda _argv, _timeout: _completed(),
            secret_runner=lambda _argv, _timeout, _secret: 0,
        )

    assert not registry.exists()


def test_registry_prefetch_cleans_registry_on_login_failure(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    monkeypatch.setattr(host, "REGISTRY_CONFIG", registry)
    monkeypatch.setattr(host, "_prepare_registry_config", lambda: registry.mkdir())

    with pytest.raises(host.ActionFailure) as failure:
        host.execute_action(
            action="prefetch-images-file",
            run_id=RUN_ID,
            primary_image=PRIMARY,
            standby_image=STANDBY,
            registry_user="octocat",
            secret_payload=base64.b64encode(b"native-gh-token") + b"\n",
            action_timeout_seconds=3570,
            runner=lambda _argv, _timeout: _completed(),
            secret_runner=lambda _argv, _timeout, _secret: 1,
        )

    assert failure.value.failure_code == "registry_auth"
    assert not registry.exists()


def test_registry_prefetch_cleans_registry_on_base_exception(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    monkeypatch.setattr(host, "REGISTRY_CONFIG", registry)
    monkeypatch.setattr(host, "_prepare_registry_config", lambda: registry.mkdir())
    monkeypatch.setattr(
        host,
        "_pull_immutable_image",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        host.execute_action(
            action="prefetch-images-file",
            run_id=RUN_ID,
            primary_image=PRIMARY,
            standby_image=STANDBY,
            registry_user="octocat",
            secret_payload=base64.b64encode(b"native-gh-token") + b"\n",
            action_timeout_seconds=3570,
            runner=lambda _argv, _timeout: _completed(),
            secret_runner=lambda _argv, _timeout, _secret: 0,
        )

    assert not registry.exists()


def test_registry_cleanup_is_idempotent_before_route_state_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(host, "REGISTRY_CONFIG", tmp_path / "missing-registry")

    report = host.execute_action(
        action="cleanup-registry",
        run_id=RUN_ID,
        runner=lambda _argv, _timeout: _completed(),
    )

    assert report["details"] == {"registry_credentials_removed": True}
