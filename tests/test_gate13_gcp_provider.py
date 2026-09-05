import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_gcp_provider as gcp
from gate13_cloud_orchestrator import PackageArtifact
from gate13_gcp_provider import GcpConfig, GcpProvider, GitHubPackageSource, LoggedRunner

RUN_ID = "g13-20260902-000000-abcd"


def artifact(platform):
    return PackageArtifact(
        platform=platform,
        source_commit="a" * 40,
        workflow_run_id=1,
        artifact_id=2,
        artifact_name=f"communityai-desktop-install-{platform}",
        wrapper_sha256="c" * 64,
        wrapper_bytes=130,
        archive_name=(
            "communityai-desktop-windows.zip" if platform == "windows" else "communityai-desktop-linux.tar.gz"
        ),
        archive_sha256="b" * 64,
        archive_bytes=123,
    )


def provider(tmp_path, runner, signed_url=lambda package: "https://productionresults.example.invalid/artifact"):
    return GcpProvider(
        run_id=RUN_ID,
        repository_root=ROOT,
        output_root=tmp_path,
        config=GcpConfig.load(ROOT / "config" / "gate13_gcp.json"),
        runner=runner,
        signed_url=signed_url,
    )


@pytest.mark.parametrize(
    "existing_changes,expired_artifact,omitted_artifact,expected_run_id",
    [
        ({}, None, None, 10),
        ({"head_sha": "b" * 40}, None, None, 11),
        ({"conclusion": "failure"}, None, None, 11),
        ({"status": "in_progress", "conclusion": None}, None, None, 11),
        ({}, "communityai-desktop-audit-linux", None, 11),
        ({}, None, "communityai-desktop-install-windows", 11),
    ],
    ids=["reuse", "different-source", "failed", "unfinished", "expired-audit", "missing-install"],
)
def test_package_reuses_only_a_complete_successful_run_of_the_pushed_source(
    tmp_path, existing_changes, expired_artifact, omitted_artifact, expected_run_id
):
    calls = []

    class Runner:
        def run(self, argv, **_kwargs):
            calls.append([str(item) for item in argv])
            return subprocess.CompletedProcess(argv, 0, "", "")

        def json(self, argv, **_kwargs):
            existing_run = "/runs/10/" in str(argv[2])
            return {
                "artifacts": [
                    {
                        "id": number,
                        "name": name,
                        "expired": existing_run and name == expired_artifact,
                    }
                    for number, name in enumerate(
                        (
                            f"communityai-desktop-{kind}-{platform}"
                            for kind in ("install", "audit")
                            for platform in ("windows", "linux")
                        ),
                        start=1,
                    )
                    if not (existing_run and name == omitted_artifact)
                ]
            }

    source = GitHubPackageSource(
        repository_root=ROOT,
        output_root=tmp_path,
        repository="flujo-app/CommunityAI",
        workflow="desktop.yaml",
        runner=Runner(),
        sleeper=lambda _seconds: None,
    )
    old = {
        "id": 10,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        **existing_changes,
    }
    fresh = {
        "id": 11,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
    }
    listings = iter([[old], [fresh]])
    source._workflow_runs = lambda _branch: next(listings)

    run_id, artifacts = source._select_or_build_run("a" * 40, "test-branch")

    assert run_id == expected_run_id
    assert len(artifacts) == 4
    assert all(item["expired"] is False for item in artifacts.values())
    assert any(command[1:3] == ["workflow", "run"] for command in calls) is (expected_run_id == 11)


def test_logged_runner_does_not_retain_argv_or_output(tmp_path):
    secret = "never-retain-this-secret"
    runner = LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None)
    result = runner.run(
        [sys.executable, "-c", f"print('{secret}')"],
        action="Safe public action",
        sensitive_output=True,
    )

    assert secret in result.stdout
    journal = (tmp_path / "journal.jsonl").read_text()
    assert secret not in journal
    assert "-c" not in journal
    assert json.loads(journal)["output_retained"] is False


def test_windows_gcloud_bypasses_cmd_argument_parsing(tmp_path, monkeypatch):
    sdk = tmp_path / "google-cloud-sdk"
    launcher = sdk / "bin" / "gcloud.cmd"
    python = sdk / "platform" / "bundledpython" / "python.exe"
    entrypoint = sdk / "lib" / "gcloud.py"
    for path in (launcher, python, entrypoint):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    monkeypatch.setattr(gcp.sys, "platform", "win32")
    monkeypatch.setattr(gcp.shutil, "which", lambda name: str(launcher) if name in {"gcloud", "gcloud.cmd"} else None)
    remote = 'powershell.exe -Command "Get-Process explorer | Where-Object {$_.Id}"'

    command = gcp._command_for_subprocess(["gcloud", "compute", "ssh", "vm", "--command", remote])

    assert command == [str(python), "-S", str(entrypoint), "compute", "ssh", "vm", "--command", remote]


def test_logged_runner_forces_noninteractive_putty_host_key_acceptance(tmp_path, monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    runner = LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None)

    runner.run([sys.executable, "-c", "pass"], action="Test command")

    assert observed["env"]["CLOUDSDK_CORE_DISABLE_PROMPTS"] == "1"
    assert observed["env"]["CLOUDSDK_SSH_PUTTY_FORCE_CONNECT"] == "1"


class CreateRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        command = [str(item) for item in argv]
        self.calls.append((command, kwargs))
        if "describe" in command and "instances" in command:
            name = command[command.index("describe") + 1]
            value = {
                "name": name,
                "labels": {"communityai_run": RUN_ID},
                "deletionProtection": False,
                "disks": [
                    {
                        "autoDelete": True,
                        "source": f"https://example.invalid/disks/{name}",
                    }
                ],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_client_creation_uses_the_proven_private_route_relay(tmp_path, monkeypatch):
    fake = CreateRunner()
    public_key = tmp_path / "test.pub"
    public_key.write_text("ssh-rsa test\n", encoding="ascii")
    relay_calls = []
    item = provider(tmp_path, fake)
    monkeypatch.setattr(item, "_ensure_ssh_key", lambda: public_key)
    monkeypatch.setattr(
        item,
        "_prepare_route_relay",
        lambda platform, package: relay_calls.append((platform, package.artifact_id))
        or "http://10.42.0.26:38081/artifact-wrapper.zip",
    )

    item.create_client("windows", artifact("windows"))

    flattened = "\n".join(" ".join(command) for command, _kwargs in fake.calls)
    assert relay_calls == [("windows", 2)]
    assert "package-url=http://10.42.0.26:38081/artifact-wrapper.zip" in flattened
    assert "package-sha256=" + "b" * 64 in flattened
    assert "package-bytes=123" in flattened
    create = next(command for command, _kwargs in fake.calls if "instances" in command and "create" in command)
    assert "--enable-display-device" in create
    assert "--no-service-account" in create
    assert "no-address" not in flattened


def test_route_creation_preserves_the_successful_private_relay_firewall(tmp_path):
    fake = CreateRunner()
    item = provider(tmp_path, fake)

    item.create_route()

    relay = next(
        command for command, _kwargs in fake.calls if "firewall-rules" in command and item.relay_firewall in command
    )
    assert relay[relay.index("--rules") + 1] == "tcp:38081"
    assert relay[relay.index("--source-tags") + 1] == item.client_tag
    assert relay[relay.index("--target-tags") + 1] == item.route
    route = next(command for command, _kwargs in fake.calls if "instances" in command and "create" in command)
    assert route[route.index("--machine-type") + 1] == "g2-standard-8"
    assert route[route.index("--boot-disk-size") + 1] == "200GB"
    assert route[route.index("--max-run-duration") + 1] == "57600s"


def test_route_relay_script_is_bound_to_both_wrapper_and_inner_archive(tmp_path):
    item = provider(
        tmp_path,
        LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None),
    )

    source = item._relay_download_script("linux", artifact("linux")).read_text()

    assert "artifact-probe-url" in source
    assert 'curl -fL --retry 4 --retry-delay 3 --silent --show-error "$url" -o "$wrapper"' in source
    assert 'test "$(stat -c %s "$wrapper")" = 130' in source
    assert "c" * 64 in source
    assert "expected = 'communityai-desktop-linux.tar.gz'" in source
    assert 'test "$(stat -c %s "$archive")" = 123' in source
    assert "b" * 64 in source


def test_route_bundle_uses_fixed_runtime_and_unchanged_signed_catalog(tmp_path):
    item = provider(
        tmp_path,
        LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None),
    )

    bundle = item._build_route_bundle()

    expected = {
        "drift-2.3.0.dev2-py3-none-any.whl": (
            389449,
            "edfd4598c293719d4d7701c9613b64f47f9fd20c3a2dc2e4c0fcacacad3c493a",
        ),
        "gate13_route_setup.sh": (
            3371,
            "f8fb52f40133fdefcc137c4244e66bec81eb5c820cf902b46b85944a4d0229e1",
        ),
        "catalog-v1.tar": (
            20480,
            "2ecf7ecbe8159d6a6328eed9b59e2a3ae4543b6ee6b82d904de42676150b1452",
        ),
    }
    for name, (byte_count, digest) in expected.items():
        payload = (bundle / name).read_bytes()
        assert len(payload) == byte_count
        assert hashlib.sha256(payload).hexdigest() == digest


def test_client_startup_scripts_are_taken_from_the_successful_run(tmp_path):
    item = provider(
        tmp_path,
        LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None),
    )

    expected = {
        "windows": (
            8779,
            "3f8600c42a3c0765e100963c2e28cdef7c6b248992924ff3406941aefce7cf47",
        ),
        "linux": (
            3808,
            "892c9d8568c491d67a7ef027177d11fc6737673f86eecd5eee9e8191ca1e8a55",
        ),
    }
    for platform, (byte_count, digest) in expected.items():
        payload = item._client_startup_script(platform).read_bytes()
        assert len(payload) == byte_count
        assert hashlib.sha256(payload).hexdigest() == digest


def test_route_preparation_waits_five_minutes_and_for_ubuntu_installer(tmp_path, monkeypatch):
    item = provider(
        tmp_path,
        LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None),
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "catalog-v1.tar").write_bytes(b"catalog")
    waits = []
    ssh_commands = []
    monkeypatch.setattr(
        item,
        "_describe_instance",
        lambda _name: {"networkInterfaces": [{"accessConfigs": [{"natIP": "198.51.100.1"}]}]},
    )
    monkeypatch.setattr(item, "_build_route_bundle", lambda: bundle)
    monkeypatch.setattr(
        item,
        "_wait_ssh",
        lambda _name, command, **_kwargs: waits.append(command),
    )
    monkeypatch.setattr(item, "_scp", lambda *_args, **_kwargs: None)

    def ssh(_name, command, **_kwargs):
        ssh_commands.append(command)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(item, "_ssh", ssh)

    result = item.prepare_route()

    assert len(waits) == 1
    assert "/proc/uptime" in waits[0]
    assert "-ge 300" in waits[0]
    assert "/var/lib/dpkg/lock-frontend" in waits[0]
    setup_command = next(command for command in ssh_commands if "gate13_route_setup.sh" in command)
    assert "install -d -m 0755 /tmp/gate13-route/catalog-v1" in setup_command
    assert len(ssh_commands) == 2
    assert result["result"] == "passed"


def test_client_readiness_cleans_the_route_relay_before_job_staging(tmp_path, monkeypatch):
    item = provider(
        tmp_path,
        LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None),
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    stage_script = stage / "stage.sh"
    stage_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(
        item,
        "_wait_ssh",
        lambda _name, command, **_kwargs: events.append(("ready", command)),
    )
    monkeypatch.setattr(
        item,
        "_cleanup_route_relay",
        lambda platform: events.append(("relay-cleaned", platform)),
    )
    monkeypatch.setattr(
        item,
        "_build_client_stage",
        lambda _platform, _package: (stage, stage_script),
    )
    monkeypatch.setattr(
        item,
        "_scp",
        lambda *_args, **_kwargs: events.append(("stage-copied", "linux")),
    )
    monkeypatch.setattr(
        item,
        "_ssh",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            "Access granted. Press Return to begin session.\n"
            "remote banner text\n"
            "GATE13_STAGE_RESULT result=passed ready=true host_user=gate13\n"
            "trailing transport text\n",
            "non-fatal transport warning\n",
        ),
    )

    result = item.prepare_client("linux", artifact("linux"))

    assert "gate13-bootstrap-ready" in events[0][1]
    assert "sudo grep -qx failed /var/lib/gate13-bootstrap-status" in events[0][1]
    assert [event[0] for event in events] == ["ready", "relay-cleaned", "stage-copied"]
    assert result["package_relay_verified"] is True
    captured = json.loads((tmp_path / "linux-stage-command-output.json").read_text())
    assert "remote banner text" in captured["stdout"]
    assert captured["stderr"] == "non-fatal transport warning\n"


def test_generated_client_jobs_are_exactly_source_and_package_bound(tmp_path):
    item = provider(
        tmp_path,
        LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None),
    )
    for platform in ("windows", "linux"):
        stage, stage_script = item._build_client_stage(platform, artifact(platform))
        lifecycle = json.loads((stage / f"gate13-{platform}-run.json").read_text())
        host = json.loads((stage / "host-job.json").read_text())
        assert lifecycle["source_commit"] == "a" * 40
        assert lifecycle["package_sha256"] == "sha256:" + "b" * 64
        assert lifecycle["package_bytes"] == 123
        assert host["source_commit"] == lifecycle["source_commit"]
        assert host["lifecycle_run_id"] == f"{RUN_ID}-{platform}"
        assert host["attempt_ordinal"] == 1
        assert stage_script.is_file()


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_failed_client_stage_retains_output_even_with_a_success_marker(tmp_path, monkeypatch, platform):
    item = provider(tmp_path, LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None))
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(item, "_wait_ssh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(item, "_cleanup_route_relay", lambda *_args: None)
    monkeypatch.setattr(item, "_build_client_stage", lambda *_args: (stage, stage / "stage-script"))
    monkeypatch.setattr(item, "_scp", lambda *_args, **_kwargs: None)

    def ssh(_instance, _command, **kwargs):
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            [], 1, "GATE13_STAGE_RESULT result=passed ready=true\n", "actual stage failure\n"
        )

    monkeypatch.setattr(item, "_ssh", ssh)
    with pytest.raises(gcp.Gate13CloudError, match="stage failed with exit code 1"):
        item.prepare_client(platform, artifact(platform))
    captured = json.loads((tmp_path / f"{platform}-stage-command-output.json").read_text())
    assert captured["exit_code"] == 1
    assert captured["stderr"] == "actual stage failure\n"


def test_client_status_poll_retries_one_temporary_connection_failure(tmp_path, monkeypatch):
    item = provider(tmp_path, LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None))
    waits = []
    messages = []
    replies = iter(
        [
            subprocess.CompletedProcess([], 0, '{"job_state":"running"}\n', ""),
            subprocess.CompletedProcess([], 1, "", "temporary DNS failure"),
            subprocess.CompletedProcess([], 0, '{"job_state":"passed"}\n', ""),
            subprocess.CompletedProcess([], 0, '{"result":"passed"}\n', ""),
            subprocess.CompletedProcess([], 0, "{}\n", ""),
        ]
    )
    monkeypatch.setattr(item, "_ssh", lambda *_args, **_kwargs: next(replies))
    item.sleeper = waits.append
    item.progress = messages.append

    payload = item.run_client("windows", artifact("windows"))

    assert payload == b'{"result":"passed"}\n'
    assert waits == [30]
    assert messages == ["Checking the windows qualification job did not complete; trying again (2 of 5)"]


def test_linux_client_reads_json_after_the_ssh_greeting(tmp_path, monkeypatch):
    item = provider(tmp_path, LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None))
    greeting = "Access granted. Press Return to begin session.\n"
    replies = iter(
        [
            subprocess.CompletedProcess([], 0, greeting + '{"job_state":"running"}\n', ""),
            subprocess.CompletedProcess([], 0, greeting + '{"job_state":"passed"}\n', ""),
            subprocess.CompletedProcess([], 0, greeting + '{"result":"passed"}\n', ""),
            subprocess.CompletedProcess([], 0, greeting + "{}\n", ""),
        ]
    )
    monkeypatch.setattr(item, "_ssh", lambda *_args, **_kwargs: next(replies))
    item.sleeper = lambda _seconds: None

    payload = item.run_client("linux", artifact("linux"))

    assert payload == b'{"result":"passed"}\n'


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_client_captures_terminal_and_stderr_before_raising(tmp_path, monkeypatch, platform):
    item = provider(tmp_path, LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None))
    calls = []
    messages = []
    item.progress = messages.append
    replies = iter(
        [
            subprocess.CompletedProcess([], 0, '{"job_state":"running"}\n', ""),
            subprocess.CompletedProcess([], 0, '{"job_state":"failed"}\n', ""),
            subprocess.CompletedProcess([], 0, '{"failure_code":"lifecycle_failed"}\n', ""),
            subprocess.CompletedProcess([], 0, "actual lifecycle error\n", ""),
            subprocess.CompletedProcess([], 0, '{"result":"failed","phase":"launch"}\n', ""),
        ]
    )

    def ssh(instance, command, **kwargs):
        calls.append((instance, command, kwargs))
        return next(replies)

    monkeypatch.setattr(item, "_ssh", ssh)

    with pytest.raises(gcp.Gate13CloudError, match="captured output"):
        item.run_client(platform, artifact(platform))

    captured = json.loads((tmp_path / f"{platform}-host-job-failure-output.json").read_text())
    assert "lifecycle_failed" in captured["terminal"]["stdout"]
    assert captured["stderr"]["stdout"] == "actual lifecycle error\n"
    assert '"phase":"launch"' in captured["evidence"]["stdout"]
    assert any("actual lifecycle error" in message for message in messages)
    observed = json.loads((tmp_path / f"{item.clients[platform]}-host-job-command-output.json").read_text())
    assert observed["stdout"] == '{"job_state":"failed"}\n'
    for call, filename in zip(calls[2:], ("terminal.json", "stderr.log", "evidence.json")):
        assert call[0] == item.clients[platform]
        if platform == "windows":
            assert call[2]["user"] == "Gate13Admin"
            script = base64.b64decode(call[1].split()[-1]).decode("utf-16-le")
            assert f"'C:\\Gate13Run\\{filename}'" in script
            assert "[IO.File]::ReadAllText" in script
        else:
            assert call[1] == f"sudo cat /qualification/{filename}"


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_failure_capture_keeps_earlier_files_when_a_later_read_times_out(tmp_path, monkeypatch, platform):
    item = provider(tmp_path, LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None))
    output_path = tmp_path / f"{platform}-host-job-failure-output.json"
    calls = []

    def ssh(_instance, _command, **_kwargs):
        calls.append(_command)
        if len(calls) == 1:
            return subprocess.CompletedProcess([], 0, '{"failure_code":"lifecycle_failed"}\n', "")
        assert "lifecycle_failed" in json.loads(output_path.read_text())["terminal"]["stdout"]
        if len(calls) == 2:
            raise gcp.CommandError("transport timed out")
        return subprocess.CompletedProcess([], 0, '{"failed_step":"initial_session"}\n', "")

    def fail(*_args):
        raise gcp.Gate13CloudError(f"{platform} host job ended in state failed")

    monkeypatch.setattr(item, "_run_client", fail)
    monkeypatch.setattr(item, "_ssh", ssh)
    with pytest.raises(gcp.Gate13CloudError, match=f"{platform} host job ended in state failed"):
        item.run_client(platform, artifact(platform))

    captured = json.loads(output_path.read_text())
    assert captured["stderr"] == {"capture_error": "CommandError"}
    assert "initial_session" in captured["evidence"]["stdout"]


def test_failure_capture_disk_error_does_not_replace_original_failure(tmp_path, monkeypatch):
    item = provider(tmp_path, LoggedRunner(tmp_path / "journal.jsonl", progress=lambda _message: None))

    def fail(*_args):
        raise gcp.Gate13CloudError("windows host job ended in state failed")

    def capture(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(item, "_run_client", fail)
    monkeypatch.setattr(item, "_capture_host_failure", capture)
    with pytest.raises(gcp.Gate13CloudError, match="windows host job ended in state failed") as error:
        item.run_client("windows", artifact("windows"))
    assert "collection failed (OSError)" in str(error.value)


def test_cleanup_instance_inspection_retries_instead_of_claiming_absence(tmp_path):
    calls = []
    waits = []

    class Runner:
        def run(self, argv, **_kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(argv, 1, "", "temporary DNS failure")
            name = str(argv[argv.index("describe") + 1])
            value = {
                "name": name,
                "labels": {"communityai_run": RUN_ID},
                "deletionProtection": False,
                "disks": [{"autoDelete": True, "source": f"https://example.invalid/disks/{name}"}],
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    item = provider(tmp_path, Runner())
    item.sleeper = waits.append

    value = item._describe_instance(item.clients["windows"], check=False)

    assert value["name"] == item.clients["windows"]
    assert len(calls) == 2
    assert waits == [15]


def test_cleanup_instance_inspection_raises_when_gcp_never_answers(tmp_path):
    calls = []

    class Runner:
        def run(self, argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "temporary DNS failure")

    item = provider(tmp_path, Runner())
    item.sleeper = lambda _seconds: None

    with pytest.raises(gcp.CommandError, match="failed after 5 attempts"):
        item._describe_instance(item.clients["windows"], check=False)

    assert len(calls) == 5


def test_cleanup_instance_inspection_accepts_an_explicit_not_found_response(tmp_path):
    calls = []

    class Runner:
        def run(self, argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "The resource was not found")

    item = provider(tmp_path, Runner())
    item.sleeper = lambda _seconds: pytest.fail("an explicit not-found response must not be retried")

    assert item._describe_instance(item.clients["windows"], check=False) is None
    assert len(calls) == 1
