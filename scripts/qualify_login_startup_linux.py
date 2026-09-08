"""Exercise the unmodified frozen Linux checkbox through isolated Xvfb/AT-SPI.

Run the host with the desktop Python environment inside a private dbus-run-session
and Xvfb. Accessibility subprocesses use system Python's python3-pyatspi. No test
telemetry, inference, sharing work, or Windows UI automation is used.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CHECKBOX_NAME = "Start CommunityAI when I sign in"
MAX_ACCESSIBLE_NODES = 4096


def private_display(display):
    if not isinstance(display, str) or re.fullmatch(r":[1-9][0-9]*", display) is None:
        raise RuntimeError("Use a private numbered Xvfb display, not an inherited host display")
    return display


def children(node):
    count = node.childCount
    if not 0 <= count <= MAX_ACCESSIBLE_NODES:
        raise RuntimeError("Accessible child count exceeds the qualification bound")
    return [node.getChildAtIndex(index) for index in range(count)]


def find_named(root, name, roles):
    pending = [root]
    found = []
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        if visited > MAX_ACCESSIBLE_NODES:
            raise RuntimeError("Accessible tree exceeds the qualification bound")
        if node.name == name and node.getRoleName() in roles:
            found.append(node)
        pending.extend(children(node))
    if len(found) != 1:
        raise RuntimeError(f"Expected exactly one {name!r} control; found {len(found)}")
    return found[0]


def select_application(desktop, pid):
    apps = [node for node in children(desktop) if node.get_process_id() == pid]
    if len(apps) != 1:
        raise RuntimeError("Expected exactly one accessibility application for the owned GUI PID")
    return apps[0]


def invoke_action(control, requested):
    action = control.queryAction()
    supported = [action.getName(index) for index in range(action.nActions)]
    acceptable = [index for index, name in enumerate(supported) if name.casefold() == requested.casefold()]
    if len(acceptable) != 1:
        raise RuntimeError(f"Expected one {requested!r} accessible action; received {supported!r}")
    selected = acceptable[0]
    if not action.doAction(selected):
        raise RuntimeError("Accessible control rejected its action")
    return supported[selected]


def accessibility_actor(pid, operation):
    import pyatspi

    deadline = time.monotonic() + 25
    app = None
    while time.monotonic() < deadline:
        try:
            app = select_application(pyatspi.Registry.getDesktop(0), pid)
            break
        except RuntimeError:
            time.sleep(0.2)
    if app is None:
        raise TimeoutError("Owned frozen GUI did not expose an AT-SPI application")
    if operation == "application":
        return {"owned_application_exposed": True}
    navigation = find_named(app, "Sharing", {"push button", "check box", "toggle button", "radio button"})
    invoke_action(navigation, "Press")
    time.sleep(0.1)
    checkbox = find_named(app, CHECKBOX_NAME, {"check box"})
    state = checkbox.getState()
    before = bool(state.contains(pyatspi.STATE_CHECKED))
    if not state.contains(pyatspi.STATE_ENABLED):
        raise RuntimeError("The actual frozen sign-in checkbox is disabled")
    selected_action = None
    if operation == "toggle":
        selected_action = invoke_action(checkbox, "Toggle")
        time.sleep(0.1)
    after = bool(checkbox.getState().contains(pyatspi.STATE_CHECKED))
    if operation == "toggle" and before == after:
        raise RuntimeError("The actual frozen checkbox did not change state")
    return {"checked_before": before, "checked_after": after, "action": selected_action}


def run_host(args):
    import psutil
    from communityai_desktop.client import NodeClient, NodeClientError
    from communityai_desktop.credentials import CredentialMissingError, NativeCredentialStore
    from communityai_desktop.startup import _linux_autostart_bytes

    if os.name != "posix" or os.geteuid() == 0:
        raise RuntimeError("Run as the ordinary Linux desktop user")
    if os.environ.get("QT_QPA_PLATFORM") != "xcb" or not os.environ.get("DISPLAY"):
        raise RuntimeError("This frozen acceptance requires the private Xvfb xcb display")
    display = private_display(os.environ["DISPLAY"])
    authority = Path(os.environ.get("XAUTHORITY", "")).resolve(strict=True)
    if not authority.is_file() or authority.stat().st_uid != os.geteuid():
        raise RuntimeError("The private Xvfb authority must belong to this ordinary user")
    owned_xvfb = []
    for process in psutil.process_iter():
        try:
            if process.name() != "Xvfb" or process.uids().real != os.geteuid():
                continue
            command = process.cmdline()
            if display in command and "-auth" in command and command[command.index("-auth") + 1] == str(authority):
                owned_xvfb.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(owned_xvfb) != 1:
        raise RuntimeError("Could not bind the display to one owned Xvfb process and its private authority")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise RuntimeError("A private accessibility/credential D-Bus session is required")
    desktop = args.desktop.resolve(strict=True)
    node = desktop.parent / "node/CommunityAI-Node"
    bootstrap = desktop.parent / "_internal/bootstrap/catalog-bootstrap.json"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config_home = Path(os.environ["XDG_CONFIG_HOME"]).resolve(strict=True)
    if not config_home.is_relative_to(output.parent) or config_home == output.parent:
        raise RuntimeError("XDG_CONFIG_HOME must be a private directory beside the evidence")
    entry = config_home / "autostart/communityai.desktop"
    if entry.exists() or entry.is_symlink():
        raise RuntimeError("Private qualification autostart entry must initially be absent")
    store = NativeCredentialStore("org.communityai.gate15.login." + output.parent.name, "control")
    try:
        store.get()
    except CredentialMissingError:
        pass
    else:
        raise RuntimeError("Qualification credential already exists")
    state = output / "state"
    config = state / "node-config.json"
    result = {
        "result": "failed",
        "scope": "unmodified-frozen-Linux-Qt-sign-in-checkbox-via-AT-SPI",
        "desktop_sha256": hashlib.sha256(desktop.read_bytes()).hexdigest(),
        "node_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "ordinary_user": True,
        "isolated_xvfb_verified": True,
        "mock_telemetry": False,
        "phases": [],
    }
    gui = None
    identities = set()
    deadline = time.monotonic() + 540

    def remaining_timeout(maximum):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Qualification deadline reached")
        return min(maximum, remaining)

    def capture():
        for pid, created in tuple(identities):
            try:
                parent = psutil.Process(pid)
                if parent.create_time() != created or not parent.is_running():
                    continue
                for process in (parent, *parent.children(recursive=True)):
                    identities.add((process.pid, process.create_time()))
            except psutil.NoSuchProcess:
                pass

    def live_owned():
        remaining = []
        for pid, created in identities:
            try:
                process = psutil.Process(pid)
                if (
                    process.create_time() == created
                    and process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                ):
                    remaining.append(process)
            except psutil.NoSuchProcess:
                pass
        return remaining

    def stop(*, cleanup_only=False):
        nonlocal gui
        capture()
        if gui is None and not live_owned():
            return
        record = {"normal_shutdown": False, "forced_cleanup": False}
        if gui is not None and gui.poll() is None:
            gui.terminate()  # The product's POSIX bridge requests Qt/node cleanup.
        normal_end = min(deadline, time.monotonic() + 35)
        while live_owned() and time.monotonic() < normal_end:
            capture()
            if gui is not None:
                gui.poll()
            time.sleep(0.2)
        returncode = None if gui is None else gui.poll()
        record["gui_returncode"] = returncode
        record["normal_shutdown"] = returncode == 0 and not live_owned()
        if not record["normal_shutdown"]:
            result["result"] = "failed"
            record["forced_cleanup"] = bool(live_owned())
        # Emergency cleanup has its own finite budget even after acceptance time
        # expires. A forced cleanup can never turn a failed shutdown into a pass.
        end = time.monotonic() + 10
        while live_owned() and time.monotonic() < end:
            capture()
            for process in reversed(live_owned()):
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
            time.sleep(0.2)
        if gui is not None:
            gui.wait(timeout=5)
        record["owned_identities_stopped"] = not live_owned()
        result.setdefault("shutdowns", []).append(record)
        if live_owned():
            raise RuntimeError("An exact owned runtime identity remains alive")
        gui = None
        if not record["normal_shutdown"] and not cleanup_only:
            raise RuntimeError("Normal frozen desktop shutdown failed; emergency cleanup recorded")

    def actor(operation):
        completed = subprocess.run(
            ["/usr/bin/python3", str(Path(__file__).resolve()), "--actor", operation, "--pid", str(gui.pid)],
            capture_output=True,
            text=True,
            timeout=remaining_timeout(35),
        )
        if completed.returncode:
            (output / f"actor-{len(result['phases'])}-failure.log").write_text(completed.stderr, encoding="utf-8")
            raise RuntimeError("AT-SPI actor failed; retained private diagnostic")
        return json.loads(completed.stdout)

    def cache_observation():
        files = [path for path in (state / "model-cache").rglob("*") if path.is_file()]
        facts = {"model_cache_file_count": len(files), "model_cache_bytes": sum(path.stat().st_size for path in files)}
        if files:
            raise RuntimeError("Unexpected model cache acquisition during sign-in qualification")
        return facts

    def launch(label, *, login=False):
        nonlocal gui
        if time.monotonic() >= deadline:
            raise TimeoutError("Qualification deadline reached")
        command = [
            str(desktop),
            "--node-url",
            args.node_url,
            "--node-config",
            str(config),
            "--node-data-dir",
            str(state),
            "--credential-service",
            store.service,
            "--credential-account",
            store.account,
        ]
        if login:
            command.append("--started-at-login")
        with (output / f"{label}.log").open("wb") as log:
            gui = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        process = psutil.Process(gui.pid)
        identities.add((process.pid, process.create_time()))
        end = min(deadline, time.monotonic() + 150)
        while time.monotonic() < end:
            if gui.poll() is not None:
                raise RuntimeError("Frozen desktop exited before readiness")
            capture()
            try:
                client = NodeClient(args.node_url, store.get(), timeout=remaining_timeout(3))
                status = client.status()
                assert status["runtime_budget"]["resident_models"] == 0
                remaining_timeout(3)
                assert client.get_contribution_policy()["policy"]["sharing_enabled"] is False
                remaining_timeout(3)
                assert all(worker["state"] == "paused" for worker in client.list_workers())
                break
            except (CredentialMissingError, NodeClientError):
                time.sleep(0.5)
        else:
            raise TimeoutError("Frozen desktop/node readiness timed out")
        result["phases"].append(
            {
                "phase": label,
                "authenticated_node_ready": True,
                "sharing_paused": True,
                "resident_models": 0,
                **cache_observation(),
            }
        )

    try:
        with (output / "bootstrap.log").open("wb") as log:
            subprocess.run(
                [str(node), "bootstrap", str(bootstrap), "--data_dir", str(state)],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=remaining_timeout(150),
                check=True,
            )
        document = json.loads(config.read_text())
        document.setdefault("contribution_policy", {})["sharing_enabled"] = False
        document["inference_mode"] = "local_only"
        config.write_text(json.dumps(document, indent=2), encoding="utf-8")
        launch("enable")
        observed = actor("toggle")
        assert observed["checked_before"] is False and observed["checked_after"] is True
        assert entry.read_bytes() == _linux_autostart_bytes((str(desktop), "--started-at-login"))
        result["phases"][-1].update(observed, exact_frozen_autostart_file=True)
        native = store.get()
        stop()
        launch("restart-and-disable")
        observed = actor("toggle")
        assert observed["checked_before"] is True and observed["checked_after"] is False
        assert not entry.exists() and store.get() == native
        result["phases"][-1].update(observed, autostart_file_removed=True, native_credential_preserved=True)
        stop()
        launch("started-at-login", login=True)
        result["phases"][-1].update(actor("application"), started_at_login_argument=True)
        stop()
        result["final_model_cache"] = cache_observation()
        result["result"] = "passed"
    except BaseException as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        raise
    finally:
        cleanup_errors = []
        try:
            stop(cleanup_only=True)
            result["recorded_runtime_identities_stopped"] = not live_owned()
        except BaseException as exc:
            cleanup_errors.append(f"runtime: {type(exc).__name__}")
            result["recorded_runtime_identities_stopped"] = False
        result["recorded_runtime_identity_count"] = len(identities)
        if result["recorded_runtime_identities_stopped"]:
            try:
                if entry.is_file() and not entry.is_symlink():
                    expected = _linux_autostart_bytes((str(desktop), "--started-at-login"))
                    if entry.read_bytes() == expected:
                        entry.unlink()
                result["private_autostart_entry_absent"] = not entry.exists() and not entry.is_symlink()
                if not result["private_autostart_entry_absent"]:
                    raise RuntimeError("Unexpected private autostart entry remains")
                store.delete()
                try:
                    store.get()
                except CredentialMissingError:
                    result["native_qualification_credential_absent"] = True
                else:
                    raise RuntimeError("Native qualification credential remains after deletion")
            except BaseException as exc:
                cleanup_errors.append(f"credential-or-entry: {type(exc).__name__}")
        else:
            result["credential_preserved_for_running_owned_runtime"] = True
        if cleanup_errors:
            result["result"] = "failed"
            result["cleanup_errors"] = cleanup_errors
        result["limitations"] = [
            "Linux Xvfb/AT-SPI, not Windows frozen-checkbox acceptance or physical desktop sign-in.",
            "Minimized/tray presentation without a window manager is not qualified; executed launch modes are in phases.",
            "No model acquisition, inference, or sharing work was requested.",
        ]
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if result["result"] != "passed":
        raise RuntimeError("Qualification or final cleanup failed; see retained evidence")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", choices=("application", "inspect", "toggle"))
    parser.add_argument("--pid", type=int)
    parser.add_argument("--desktop", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--node-url", default="http://127.0.0.1:18108")
    args = parser.parse_args()
    if args.actor:
        if args.pid is None or args.pid <= 0:
            parser.error("--actor requires a positive --pid")
        print(json.dumps(accessibility_actor(args.pid, args.actor)))
    else:
        if args.desktop is None or args.output is None:
            parser.error("--desktop and --output are required")
        print(json.dumps({"result": run_host(args)["result"]}))
