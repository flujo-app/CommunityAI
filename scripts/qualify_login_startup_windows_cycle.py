"""Opt-in frozen Windows login-startup cycle on unswitched private desktops."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import qualify_login_startup_windows as probe
from communityai_desktop.credentials import CredentialMissingError, NativeCredentialStore
from communityai_desktop.startup import WINDOWS_RUN_KEY, WINDOWS_VALUE_NAME, login_startup_command
from qualify_login_startup_windows_state import RunValueGuard


def write_run(value):
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, WINDOWS_VALUE_NAME, 0, value[1], value[0])


def delete_run():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, WINDOWS_VALUE_NAME)
    except FileNotFoundError:
        pass


def finalize_cycle(result, output, guard, store, credential_created):
    """Restore after proven process shutdown, retaining unconditional evidence."""
    try:
        stopped = all(phase.get("owned_processes_stopped") is True for phase in result["phases"])
        result["owned_processes_stopped"] = stopped
        result["original_run_state_restored"] = False
        if stopped:
            try:
                result["restoration_required_registry_write"] = guard.restore()
                result["original_run_state_restored"] = True
            except BaseException as exc:
                result.update(result="failed", restoration_error_type=type(exc).__name__)
        else:
            result.update(result="failed", restoration_deferred_for_unverified_processes=True)
        if credential_created:
            result["private_credential_removed"] = False
            if stopped:
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
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def run_cycle(args):
    if not args.exercise_login_startup:
        raise ValueError("The real login-startup cycle requires explicit opt-in")
    if probe.qualification_platform() != "Windows":
        raise RuntimeError("The private-desktop login cycle requires Windows")
    probe.require_desktop_stopped()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    desktop = args.desktop.resolve()
    node = desktop.parent / "node/CommunityAI-Node.exe"
    bootstrap = desktop.parent / "_internal/bootstrap/catalog-bootstrap.json"
    assert bootstrap.read_bytes() == (probe.ROOT / "public-alpha/catalog-qwen-v2/catalog-bootstrap.json").read_bytes()
    guard = RunValueGuard(probe.run_value, write_run, delete_run)
    # Private recovery record; binary registry data is encoded without evaluating it.
    (output / "original-run.private.json").write_text(
        json.dumps(guard.original, default=lambda value: {"bytes_hex": value.hex()}) + "\n"
    )
    expected = (subprocess.list2cmdline(login_startup_command(desktop, frozen=True)), 1)  # REG_SZ
    guard.expect(expected)
    guard.expect(None)
    store = NativeCredentialStore("org.communityai.private-desktop." + output.name, "control")
    try:
        store.get()
    except CredentialMissingError:
        pass
    else:
        raise RuntimeError("The private qualification credential already exists")
    state = output / "state"
    result = {
        "result": "failed",
        "scope": "frozen-Windows-private-desktop-login-enable-restart-disable",
        "explicit_mutation_opt_in": True,
        "run_entry_original_present": guard.original is not None,
        "expected_startup_argv": list(login_startup_command(desktop, frozen=True)),
        "expected_run_type": "REG_SZ",
        "credential_service": store.service,
        "credential_account": store.account,
        "replay_sha256": probe.sha256(Path(__file__)),
        "run_state_guard_sha256": probe.sha256(Path(__file__).with_name("qualify_login_startup_windows_state.py")),
        "phases": [],
        "actual_os_sign_in_exercised": False,
    }
    credential_created = False
    try:
        environment = os.environ.copy()
        environment.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
        initialized = subprocess.run(
            [str(node), "bootstrap", str(bootstrap), "--data_dir", str(state)],
            capture_output=True,
            timeout=90,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (output / "bootstrap.private.log").write_bytes(initialized.stdout + initialized.stderr)
        assert initialized.returncode == 0, "Signed bootstrap failed; private log retained"
        assert json.loads(initialized.stdout.decode().splitlines()[-1])["catalog_sequence"] == 2
        config_path = state / "node-config.json"
        config = json.loads(config_path.read_text())
        config["inference_mode"] = "local_only"
        config["contribution_policy"]["sharing_enabled"] = False
        config_path.write_text(json.dumps(config, indent=2))
        config_before = config_path.read_bytes()
        credential_created = True
        store.provision(state / "unused-private-legacy-key", allow_create=True)
        original_credential = store.get()
        for action, before, after, initial, final in (
            ("enable", guard.original, expected, "Off", "On"),
            ("disable", expected, None, "On", "Off"),
        ):
            phase_output = output / action
            phase_record = {"action": action, "result": "failed", "owned_processes_stopped": True}
            result["phases"].append(phase_record)
            guard.verify(before)
            assert store.get() == original_credential
            phase_args = SimpleNamespace(
                desktop=desktop,
                output=phase_output,
                node_url=args.node_url,
                action=action,
                shared_state=state,
                shared_store=store,
                before_action=lambda before=before: guard.verify(before),
                after_action=lambda after=after: guard.verify(after),
                expected_run_after=after,
            )
            phase_record["owned_processes_stopped"] = False
            try:
                phase = probe.run(phase_args)
            finally:
                if (phase_output / "result.json").exists():
                    phase_record.update(json.loads((phase_output / "result.json").read_text()))
            assert phase["result"] == "passed"
            assert phase["uia"]["initial_state"] == initial and phase["uia"]["state"] == final
            assert phase["uia"]["login_action"] == action and phase["uia"]["registry_mutation"] == "True"
            assert phase["owned_processes_stopped"] is True
            assert store.get() == original_credential
            assert config_path.read_bytes() == config_before
            phase_record["native_credential_unchanged_after_shutdown"] = True
            phase_record["private_config_unchanged_after_shutdown"] = True
        result.update(result="passed", restart_preserved_enabled_checkbox=True, private_credential_continuity=True)
    except BaseException as exc:
        result.update(result="failed", error_type=type(exc).__name__)
        raise
    finally:
        finalize_cycle(result, output, guard, store, credential_created)
    return result
