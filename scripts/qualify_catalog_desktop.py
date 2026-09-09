"""Replay an ordinary-user frozen Windows/Linux desktop's signed catalog migration.

Uses a new private state directory and native credential account. Sharing remains
off; no inference request, catalog publication, or installer action is performed.
Qt runs offscreen unless --visible-ui explicitly requests native-window acceptance.
"""

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path

import psutil
from communityai_desktop.client import NodeClient, NodeClientError
from communityai_desktop.credentials import CredentialMissingError, NativeCredentialStore

ROOT = Path(__file__).resolve().parents[1]


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-url", default="http://127.0.0.1:18104")
    parser.add_argument("--visible-ui", action="store_true", help="Explicitly open native qualification windows")
    return parser


def desktop_environment(visible_ui, *, system=None):
    environment = os.environ.copy()
    system = platform.system() if system is None else system
    if visible_ui and system != "Windows":
        raise ValueError("Native-window observation currently supports Windows; use offscreen on Linux")
    environment["QT_QPA_PLATFORM"] = "windows" if visible_ui else "offscreen"
    return environment


def qualification_platform():
    system = platform.system()
    if system == "Windows":
        if ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("Run this acceptance from an ordinary, non-elevated Windows session")
    elif system == "Linux":
        if os.geteuid() == 0:
            raise RuntimeError("Run this acceptance as an ordinary Linux user with a native credential store")
    else:
        raise RuntimeError("This acceptance supports Windows and Linux")
    return system


def node_executable(desktop, system):
    return desktop.parent / "node" / ("CommunityAI-Node.exe" if system == "Windows" else "CommunityAI-Node")


def identity_is_live(process, created):
    # A Linux zombie cannot execute or retain an open model runtime. Its parent
    # or container init still owns reaping the remaining process-table entry.
    return process.create_time() == created and process.is_running() and process.status() != psutil.STATUS_ZOMBIE


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def process_tree(pid):
    process = psutil.Process(pid)
    identities = []
    for child in (process, *process.children(recursive=True)):
        try:
            identities.append((child.pid, child.create_time()))
        except psutil.NoSuchProcess:
            pass
    return identities


def wait_tree_gone(identities):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        remaining = []
        for pid, created in identities:
            try:
                process = psutil.Process(pid)
                if identity_is_live(process, created):
                    remaining.append(pid)
            except psutil.NoSuchProcess:
                pass
        if not remaining:
            return
        time.sleep(0.2)
    raise AssertionError(f"Recorded desktop tree remains alive: {remaining}")


def force_stop_owned_tree(identities, *, timeout=15):
    """Bound emergency cleanup to recorded identities and their descendants.

    A PID alone is insufficient: a reused PID must never be signaled. psutil's
    terminate/kill methods also guard identity reuse immediately before signaling.
    Keep discovering children while an owned ancestor remains alive so a helper
    created during shutdown cannot escape a fixed initial snapshot.
    """
    known = set(identities)
    signaled = set()
    deadline = time.monotonic() + timeout
    while True:
        live = {}
        for pid, created in tuple(known):
            try:
                process = psutil.Process(pid)
                if not identity_is_live(process, created):
                    continue
                live[(pid, created)] = process
                for child in process.children(recursive=True):
                    identity = (child.pid, child.create_time())
                    known.add(identity)
                    if identity_is_live(child, identity[1]):
                        live[identity] = child
            except psutil.NoSuchProcess:
                continue
        identities[:] = sorted(known)
        if not live:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned desktop cleanup exceeded its deadline")
        for identity, process in reversed(tuple(live.items())):
            try:
                if identity not in signaled:
                    process.terminate()
                    signaled.add(identity)
                else:
                    process.kill()
            except psutil.NoSuchProcess:
                pass
        time.sleep(0.2)


def visible_window(pid):
    if platform.system() != "Windows":
        return False
    found = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def observe(window, unused):
        owner = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and ctypes.windll.user32.IsWindowVisible(window):
            title = ctypes.create_unicode_buffer(512)
            ctypes.windll.user32.GetWindowTextW(window, title, len(title))
            if title.value == "CommunityAI":
                found.append(True)
        return True

    ctypes.windll.user32.EnumWindows(observe, 0)
    return bool(found)


def require_desktop_stopped():
    for process in psutil.process_iter(("name",)):
        if (process.info["name"] or "").casefold() in ("communityai.exe", "communityai"):
            try:
                if process.status() != psutil.STATUS_ZOMBIE:
                    raise RuntimeError("Close the existing CommunityAI desktop before this isolated replay")
            except psutil.NoSuchProcess:
                continue


def run(args):
    system = qualification_platform()
    environment = desktop_environment(args.visible_ui, system=system)
    require_desktop_stopped()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    desktop = args.desktop.resolve()
    node = node_executable(desktop, system)
    bootstrap = desktop.parent / "_internal/bootstrap/catalog-bootstrap.json"
    assert bootstrap.read_bytes() == (ROOT / "public-alpha/catalog-qwen-v2/catalog-bootstrap.json").read_bytes()
    state = output / "state"
    config_path = state / "node-config.json"
    store = NativeCredentialStore("org.communityai.catalog-acceptance." + output.name, "control")
    try:
        store.get()
    except CredentialMissingError:
        pass
    else:
        raise RuntimeError("The private qualification credential already exists")
    process_options = {"creationflags": subprocess.CREATE_NO_WINDOW} if system == "Windows" else {}
    result = {
        "result": "failed",
        "scope": f"ordinary-user-frozen-{system}-desktop-signed-catalog-startup-migration",
        "platform": system,
        "non_elevated": True,
        "zombies_treated_as_non_executing": system == "Linux",
        "ui_mode": "visible-native" if args.visible_ui else "offscreen",
        "desktop_sha256": sha256(desktop),
        "node_sha256": sha256(node),
        "bootstrap_sha256": sha256(bootstrap),
        "replay_script_sha256": sha256(Path(__file__)),
        "launches": [],
    }
    gui = None
    identities = []

    def stop():
        nonlocal gui
        if gui is None:
            return
        try:
            if gui.poll() is None:
                identities.extend(process_tree(gui.pid))
                subprocess.run(
                    [str(desktop), "--prepare-update"], check=True, timeout=60, env=environment, **process_options
                )
            assert gui.wait(timeout=60) == 0
            wait_tree_gone(identities)
        except BaseException as exc:
            result["result"] = "failed"
            result["shutdown_failure_type"] = type(exc).__name__
            try:
                force_stop_owned_tree(identities)
                gui.wait(timeout=5)
            except BaseException as cleanup_error:
                result["owned_shutdown_fallback"] = "failed"
                result["cleanup_failure_type"] = type(cleanup_error).__name__
                raise
            else:
                result["owned_shutdown_fallback"] = "passed"
                gui = None
            raise
        gui = None

    def launch(label):
        nonlocal gui
        started = time.monotonic()
        with (output / f"{label}.log").open("wb") as log:
            gui = subprocess.Popen(
                [
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
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                **process_options,
            )
        # Record ownership immediately, including failures before authenticated
        # readiness. Later snapshots expand this set with owned descendants.
        identities.append((gui.pid, psutil.Process(gui.pid).create_time()))
        deadline = started + 240
        while time.monotonic() < deadline:
            if gui.poll() is not None:
                raise RuntimeError("Frozen desktop exited before authenticated readiness")
            try:
                client = NodeClient(args.node_url, store.get(), timeout=5)
                status = client.status()
                if not args.visible_ui or visible_window(gui.pid):
                    break
            except (CredentialMissingError, NodeClientError):
                pass
            time.sleep(0.5)
        else:
            raise TimeoutError("Frozen desktop did not open and expose its authenticated node")
        identities.extend(process_tree(gui.pid))
        workers = client.list_workers()
        assert all(worker["state"] == "paused" for worker in workers)
        assert client.get_contribution_policy()["policy"]["sharing_enabled"] is False
        record = {
            "phase": label,
            "seconds_to_authenticated_status": round(time.monotonic() - started, 3),
            "visible_native_window": visible_window(gui.pid),
            "model_ids": [model["id"] for model in status["models"]],
            "worker_states": [worker["state"] for worker in workers],
            "owned_process_count": len(set(identities)),
        }
        result["launches"].append(record)
        print(json.dumps(record), flush=True)

    try:
        proc = subprocess.run(
            [
                str(node),
                "bootstrap",
                str(ROOT / "public-alpha/catalog-v1/catalog-bootstrap.json"),
                "--data_dir",
                str(state),
            ],
            capture_output=True,
            timeout=300,
            **process_options,
        )
        (output / "legacy-install.log").write_bytes(proc.stdout + proc.stderr)
        assert proc.returncode == 0, "Legacy signed catalog bootstrap failed; see retained log"
        result["legacy_install"] = json.loads(proc.stdout.decode().splitlines()[-1])
        assert result["legacy_install"]["catalog_sequence"] == 1
        legacy = json.loads(config_path.read_text())
        legacy["inference_mode"] = "local_only"
        legacy["contribution_policy"] = {
            "sharing_enabled": False,
            "max_vram": "37%",
            "max_processing_percent": 43,
            "max_disk_space": "8GiB",
        }
        config_path.write_text(json.dumps(legacy, indent=2))
        sentinel = state / "model-cache/retained-cache-sentinel"
        sentinel.parent.mkdir(exist_ok=True)
        sentinel.write_bytes(b"Ordinary user retained cache choice\n")
        sentinel_digest = sha256(sentinel)
        launch("automatic-migration")
        migrated = json.loads(config_path.read_text())
        assert migrated["inference_mode"] == legacy["inference_mode"]
        assert migrated["contribution_policy"] == legacy["contribution_policy"]
        assert migrated["workers"] == legacy["workers"]
        assert sha256(sentinel) == sentinel_digest
        assert (
            Path(migrated["catalog_path"]).read_bytes()
            == (ROOT / "public-alpha/catalog-qwen-v2/catalog.signed.json").read_bytes()
        )
        assert (
            json.loads(Path(migrated["catalog_bootstrap_path"]).read_text())["trust_root"]
            == json.loads(bootstrap.read_text())["trust_root"]
        )
        assert set(migrated["auto_model_priority"]) == {
            "sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4",
            "sha256:e62b19ad7d0c6af3dabe730105aefd4cf067ddc50063ffa74c00bd94a29bd7d0",
        }
        result["activated_exact_published_sequence_2"] = True
        result["preferences_workers_and_cache_preserved"] = True
        native_credential = store.get()
        before = config_path.read_bytes()
        stop()
        launch("repeat-start")
        assert config_path.read_bytes() == before
        assert store.get() == native_credential
        result["repeat_start_config_and_native_credential_preserved"] = True
        stop()
        proc = subprocess.run(
            [
                str(node),
                "bootstrap",
                str(ROOT / "public-alpha/catalog-v1/catalog-bootstrap.json"),
                "--data_dir",
                str(state),
                "--refresh",
            ],
            capture_output=True,
            timeout=300,
            **process_options,
        )
        (output / "old-root-rejected.log").write_bytes(proc.stdout + proc.stderr)
        assert proc.returncode != 0 and config_path.read_bytes() == before
        result["old_trust_root_rejected_without_config_change"] = True
        result["result"] = "passed"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        shutdown_complete = False
        try:
            stop()
            wait_tree_gone(identities)
            result["recorded_runtime_trees_gone"] = True
            shutdown_complete = True
        except BaseException as cleanup_error:
            result["result"] = "failed"
            result["recorded_runtime_trees_gone"] = False
            result["cleanup_failure_type"] = type(cleanup_error).__name__
            raise
        finally:
            if shutdown_complete:
                try:
                    store.delete()
                    store.get()
                except CredentialMissingError:
                    result["native_qualification_credential_removed"] = True
                except BaseException as credential_error:
                    result["result"] = "failed"
                    result["native_qualification_credential_removed"] = False
                    result["credential_cleanup_failure_type"] = type(credential_error).__name__
                else:
                    result["result"] = "failed"
                    result["native_qualification_credential_removed"] = False
                    result["credential_cleanup_failure_type"] = "CredentialStillPresent"
            else:
                # Retain access until a remaining owned runtime can be shut down;
                # never claim cleanup because its credential was merely erased.
                result["native_qualification_credential_removed"] = False
                result["credential_preserved_for_cleanup"] = True
            result["limitations"] = [
                f"{system} startup migration and unchanged-catalog restart only; other platforms require separate replay.",
                "The legacy state was installed from the actual signed online catalog, with private test preferences/cache marker.",
                "No inference, worker execution, periodic newer-sequence activation, or active-generation drain was exercised.",
                "The existing frozen bundle was launched directly; its installer lifecycle has separate evidence.",
            ]
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps({"result": result["result"], "output": str(output)}), flush=True)

    if result["result"] != "passed":
        raise RuntimeError("Catalog replay did not complete verified cleanup")


if __name__ == "__main__":
    run(build_parser().parse_args())
