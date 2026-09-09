"""Read the frozen Windows sign-in checkbox on an unswitched private desktop.

Default read mode never changes the real Run value. Explicit
--exercise-login-startup enables the guarded on/restart/off acceptance. Both use
fresh signed private state, a paused managed node, and a unique native credential.
The companion UIA process uses no input injection or input-desktop switching.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import psutil
from communityai_desktop.client import NodeClient, NodeClientError
from communityai_desktop.credentials import CredentialMissingError, NativeCredentialStore
from communityai_desktop.startup import WINDOWS_RUN_KEY, WINDOWS_VALUE_NAME
from qualify_catalog_desktop import (
    force_stop_owned_tree,
    identity_is_live,
    process_tree,
    qualification_platform,
    require_desktop_stopped,
    sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def run_value():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
            return winreg.QueryValueEx(key, WINDOWS_VALUE_NAME)
    except FileNotFoundError:
        return None


def fields(path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def owned_gui_is_live(identity):
    if identity is None:
        return False
    try:
        return identity_is_live(psutil.Process(identity[0]), identity[1])
    except psutil.NoSuchProcess:
        return False


def refresh_owned_identities(identities):
    known = set(identities)
    for pid, created in tuple(known):
        try:
            process = psutil.Process(pid)
            if identity_is_live(process, created):
                for child in process.children(recursive=True):
                    known.add((child.pid, child.create_time()))
        except psutil.NoSuchProcess:
            pass
    identities[:] = sorted(known)


def finalize_evidence(
    result,
    output,
    store,
    credential_created,
    original,
    identities,
    *,
    read_run=run_value,
    comparison_field="run_entry_unchanged",
):
    try:
        try:
            helper_result = output / "helper-result.txt"
            if helper_result.exists():
                result["private_desktop_helper"] = fields(helper_result)
            for filename, label in (("launch.txt", "gui_launch"), ("actor-launch.txt", "uia_actor_launch")):
                path = output / filename
                if path.exists():
                    lines = path.read_text(encoding="utf-8").splitlines()
                    result[label] = {"pid": int(lines[0]), "creation_filetime": int(lines[1])}
            if result.get("helper_containment_required") and (
                result.get("private_desktop_helper", {}).get("job_cleanup_verified") != "True"
            ):
                result.update(result="failed", owned_processes_stopped=False, job_cleanup_unverified=True)
        except BaseException as exc:
            result.update(result="failed", owned_processes_stopped=False, helper_evidence_error_type=type(exc).__name__)
        try:
            result[comparison_field] = read_run() == original
            if not result[comparison_field]:
                result["result"] = "failed"
        except BaseException as exc:
            result[comparison_field] = False
            result.update(result="failed", run_read_error_type=type(exc).__name__)
        if credential_created:
            result["private_credential_removed"] = False
            if result.get("owned_processes_stopped"):
                try:
                    store.delete()
                    try:
                        store.get()
                    except CredentialMissingError:
                        result["private_credential_removed"] = True
                    else:
                        result.update(result="failed", credential_cleanup_error_type="CredentialStillPresent")
                except BaseException as exc:
                    result.update(result="failed", credential_cleanup_error_type=type(exc).__name__)
            else:
                result.update(result="failed", private_credential_retained_for_live_processes=True)
    finally:
        result["owned_process_identities"] = sorted(set(identities))
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def wait_owned_gone(identities, deadline):
    while True:
        live = []
        for pid, created in set(identities):
            try:
                if identity_is_live(psutil.Process(pid), created):
                    live.append(pid)
            except psutil.NoSuchProcess:
                pass
        if not live:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned process shutdown exceeded the shared probe deadline")
        time.sleep(0.1)


def run(args):
    assert qualification_platform() == "Windows"
    require_desktop_stopped()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    desktop = args.desktop.resolve()
    node = desktop.parent / "node/CommunityAI-Node.exe"
    bootstrap = desktop.parent / "_internal/bootstrap/catalog-bootstrap.json"
    assert bootstrap.read_bytes() == (ROOT / "public-alpha/catalog-qwen-v2/catalog-bootstrap.json").read_bytes()
    original = run_value()
    action = getattr(args, "action", "read")
    shared_state = getattr(args, "shared_state", None)
    store = getattr(args, "shared_store", None) or NativeCredentialStore(
        "org.communityai.private-desktop." + output.name, "control"
    )
    if shared_state is None:
        try:
            store.get()
        except CredentialMissingError:
            pass
        else:
            raise RuntimeError("The private qualification credential already exists")
    else:
        store.get()
    if action != "read":
        assert shared_state is not None and action in ("enable", "disable")
        assert callable(args.before_action) and callable(args.after_action)
    result = {
        "result": "failed",
        "scope": "frozen-Windows-private-desktop-login-checkbox-" + action,
        "desktop_sha256": sha256(desktop),
        "node_sha256": sha256(node),
        "bootstrap_sha256": sha256(bootstrap),
        "replay_sha256": sha256(Path(__file__)),
        "uia_source_sha256": sha256(Path(__file__).with_suffix(".cs")),
        "non_elevated": True,
        "run_entry_original_present": original is not None,
        "registry_mutation": action != "read",
        "visible_input_desktop_acceptance": False,
        "gui_probe_deadline_seconds": 120,
    }
    state = Path(shared_state) if shared_state is not None else output / "state"
    config_path = state / "node-config.json"
    identities = []
    helper = None
    gui_identity = None
    gui_deadline = None
    credential_created = False
    environment = os.environ.copy()
    environment.update(QT_QPA_PLATFORM="windows", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        compiler_root = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
        compiled = output / "private-desktop-reader.exe"
        subprocess.run(
            [
                str(compiler_root / "csc.exe"),
                "/nologo",
                "/target:winexe",
                "/out:" + str(compiled),
                *[
                    "/reference:" + str(compiler_root / "WPF" / name)
                    for name in ("UIAutomationClient.dll", "UIAutomationTypes.dll", "WindowsBase.dll")
                ],
                str(Path(__file__).with_suffix(".cs").resolve()),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            **flags,
        )
        if shared_state is None:
            proc = subprocess.run(
                [str(node), "bootstrap", str(bootstrap), "--data_dir", str(state)],
                capture_output=True,
                timeout=90,
                env=environment,
                **flags,
            )
            (output / "bootstrap.private.log").write_bytes(proc.stdout + proc.stderr)
            assert proc.returncode == 0, "Signed bootstrap failed; private log retained"
            installed = json.loads(proc.stdout.decode().splitlines()[-1])
            assert installed["catalog_sequence"] == 2
            config = json.loads(config_path.read_text())
            config["inference_mode"] = "local_only"
            config["contribution_policy"]["sharing_enabled"] = False
            config_path.write_text(json.dumps(config, indent=2))
            credential_created = True
            store.provision(state / "unused-private-legacy-key", allow_create=True)
        command = [
            str(desktop),
            "--node-url",
            args.node_url,
            "--node-config",
            str(config_path),
            "--node-data-dir",
            str(state),
            "--credential-service",
            store.service,
            "--credential-account",
            store.account,
        ]
        command_file = output / "command.private.txt"
        command_file.write_text(subprocess.list2cmdline(command))
        started = time.monotonic()
        gui_deadline = started + 120
        helper = subprocess.Popen(
            [str(compiled), str(desktop), str(command_file), str(output), action],
            env=environment,
            **flags,
        )
        result["helper_containment_required"] = True
        identities.append((helper.pid, psutil.Process(helper.pid).create_time()))
        status = None
        gui = None
        while time.monotonic() - started < 75:
            if helper.poll() is not None:
                raise RuntimeError("Private desktop helper exited before acceptance")
            identities.extend(process_tree(helper.pid))
            if action != "read" and (output / "action-ready.txt").exists() and not (output / "allow-action").exists():
                assert (output / "action-ready.txt").read_text() == action
                args.before_action()
                (output / "allow-action").touch()
            launch = output / "launch.txt"
            if gui is None and launch.exists():
                pid, filetime, private_name, input_name = launch.read_text().splitlines()
                gui = psutil.Process(int(pid))
                assert abs(gui.create_time() - (int(filetime) / 10000000 - 11644473600)) < 0.001
                identities.append((gui.pid, gui.create_time()))
                gui_identity = (gui.pid, gui.create_time())
                result.update(private_desktop_name=private_name, input_desktop_before=input_name)
            try:
                client = NodeClient(args.node_url, store.get(), timeout=2)
                status = client.status()
                result["last_authenticated_status"] = {
                    "status": status["status"],
                    "resident_models": status["runtime_budget"]["resident_models"],
                    "worker_states": [worker["state"] for worker in status["workers"]],
                }
                if (output / "uia.txt").exists():
                    break
            except NodeClientError:
                pass
            time.sleep(0.2)
        else:
            raise TimeoutError("Frozen Qt checkbox/authenticated node exceeded observation deadline")
        assert status["status"] == "running"
        assert status["runtime_budget"]["resident_models"] == 0
        assert status["inference_mode"] == "local_only"
        assert not status["contribution"]["policy"]["policy"]["sharing_enabled"]
        assert all(worker["state"] == "paused" for worker in status["workers"])
        assert not list(state.rglob("*.safetensors"))
        result.update(
            seconds_to_uia_and_authenticated_status=round(time.monotonic() - started, 3),
            uia=fields(output / "uia.txt"),
            resident_models=0,
            private_model_weight_files=0,
            worker_states=[worker["state"] for worker in status["workers"]],
            catalog_sequence=2,
        )
        assert result["uia"]["result"] == "passed"
        assert result["uia"]["control_type"] == "ControlType.CheckBox"
        if action == "read":
            assert run_value() == original
        else:
            args.after_action()
        result["result"] = "passed"
    except BaseException as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        try:
            if helper is not None:
                refresh_owned_identities(identities)
                # Existing desktop guard ensures this maintenance message targets
                # only the GUI launched above. It creates no normal app window.
                if owned_gui_is_live(gui_identity):
                    maintenance = subprocess.Popen([str(desktop), "--prepare-update"], env=environment, **flags)
                    identities.append((maintenance.pid, psutil.Process(maintenance.pid).create_time()))
                    assert maintenance.wait(timeout=min(30, max(0.1, gui_deadline - time.monotonic() - 10))) == 0
                refresh_owned_identities(identities)
                app_identities = [item for item in set(identities) if item[0] != helper.pid]
                wait_owned_gone(app_identities, gui_deadline - 5)
                (output / "stop").touch()
                assert helper.wait(timeout=min(5, max(0.1, gui_deadline - time.monotonic()))) == 0
                wait_owned_gone(identities, gui_deadline)
                result["private_desktop_helper"] = fields(output / "helper-result.txt")
                assert result["private_desktop_helper"]["input_desktop_unchanged"] == "True"
                assert result["private_desktop_helper"]["desktop_handle_closed"] == "True"
                assert result["private_desktop_helper"]["job_assigned_before_resume"] == "True"
                assert result["private_desktop_helper"]["actor_job_assigned_before_resume"] == "True"
                assert result["private_desktop_helper"]["job_active_processes_at_close"] == "0"
                assert result["private_desktop_helper"]["job_handle_closed"] == "True"
            result["owned_processes_stopped"] = True
        except BaseException as exc:
            result["result"] = "failed"
            result["cleanup_error_type"] = type(exc).__name__
            force_stop_owned_tree(identities, timeout=max(1, min(10, gui_deadline - time.monotonic())))
            wait_owned_gone(identities, gui_deadline)
            result["owned_processes_stopped"] = True
            raise
        finally:
            finalize_evidence(
                result,
                output,
                store,
                credential_created,
                original if action == "read" else args.expected_run_after,
                identities,
                comparison_field="run_entry_unchanged" if action == "read" else "expected_run_state_verified",
            )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-url", default="http://127.0.0.1:18118")
    parser.add_argument("--exercise-login-startup", action="store_true")
    args = parser.parse_args()
    if args.exercise_login_startup:
        from qualify_login_startup_windows_cycle import run_cycle

        answer = run_cycle(args)
    else:
        answer = run(args)
    print(json.dumps({"result": answer["result"]}))
    raise SystemExit(0 if answer["result"] == "passed" else 1)
