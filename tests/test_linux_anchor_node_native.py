"""Actual native birth/control/journal/trees, with fixture systemd and node bodies."""

import json
import os
import select
import signal
import socket
import sys
import threading
import time
import types
from pathlib import Path
from uuid import uuid4

if __name__ == "__main__":
    import types

    for name, folder in (("drift", "drift"), ("drift.node", "drift/node")):
        module = types.ModuleType(name)
        module.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / folder)]
        sys.modules[name] = module
    # Runtime metadata fixture only; no actual model execution in this driver.
    sys.modules["drift"].__version__ = "2.3.0.dev2"
else:
    import pytest

from drift.node import linux_anchor as anchor, linux_anchor_control as control, linux_cgroup_process as native
from drift.node.linux_anchor_entry import NODE_TOKEN_ENV
from drift.node.linux_anchor_node import AnchorNode
from drift.node.resource_recovery import RecoverableStateError


def properties(pid, group):
    return dict(
        Id=anchor.UNIT,
        LoadState="loaded",
        ActiveState="active",
        SubState="running",
        MainPID=str(pid),
        ControlGroup=group,
        InvocationID="a" * 32,
        Delegate="yes",
        Type="exec",
        KillMode="control-group",
        Restart="no",
        Transient="no",
    )


def body(profile, group, worker_root, mode):
    anchor._query_properties = lambda: properties(os.getppid(), group)
    import launch_volunteer_node as launcher
    from communityai_desktop.profiles import VolunteerProfile

    # Isolated profile location and controlled model body only. The real
    # launcher/entry checks, native process identity and state are exercised.
    VolunteerProfile.for_current_user = classmethod(lambda cls: cls(profile))
    runtime = types.ModuleType("launch_node")
    runtime.main = lambda: node_runtime(profile, worker_root, mode)
    sys.modules["launch_node"] = runtime
    if mode == "forged_token":
        token = os.environ[NODE_TOKEN_ENV]
        os.environ[NODE_TOKEN_ENV] = ("0" if token[0] != "0" else "1") + token[1:]
    if mode == "forged_lifetime":
        import fcntl

        marker = profile / "node-lifetime.lock"
        marker.rename(marker.with_name("retained-lifetime.lock"))
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return launcher.main(["--worker-cgroup-root", worker_root + ("/other" if mode == "wrong_root" else "")])


def lose_journal(profile, replace):
    journal = profile / "node" / "resource-reservations"
    if replace:
        journal.rename(journal.with_name("retained-journal"))
        journal.mkdir(mode=0o700)
    else:
        (journal / "admission.lock").unlink()
        (journal / "generations.json").unlink()


def spawn_worker(profile, worker_root, *, lose_after_binding=None):
    from types import SimpleNamespace

    from drift.node.host_resources import canonical_cache_root
    from drift.node.linux_anchor_entry import reservation_storage_binding
    from drift.node.placement_resources import (
        ArtifactClaim,
        CacheSnapshot,
        ResourceSnapshot,
        VolumeSnapshot,
        WorkerResourceClaim,
    )
    from drift.node.resource_reservations import ResourceReservationManager

    cache = profile / "cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    root = canonical_cache_root(cache)

    def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
        return ResourceSnapshot(
            now,
            host_limit_bytes,
            10000,
            tuple(CacheSnapshot(p, "disk", 0, limit) for p, limit in cache_limits.items()),
            (VolumeSnapshot("disk", 10000),),
        )

    launch = SimpleNamespace(
        worker_id="gpu-0",
        resource_claim=WorkerResourceClaim(
            "template", "gpu-0", 100, 200, (ArtifactClaim(root, "weight.bin", "a" * 64, 11),)
        ),
        max_host_memory_bytes=10000,
        max_disk_bytes=1000,
        placement_cache_root=root,
        placement_manifest_digest="sha256:" + "b" * 64,
        placement_artifact_set_digest="c" * 64,
        placement_artifact_bytes=11,
        block_indices="0:1",
    )
    storage_binding = reservation_storage_binding(profile / "node" / "resource-reservations", worker_root)
    if lose_after_binding is not None:
        lose_journal(profile, lose_after_binding)
    manager = ResourceReservationManager(
        profile / "node" / "resource-reservations",
        snapshot_provider=snapshot,
        loading_protocol=True,
        recovery_protocol=True,
        worker_cgroup_root=worker_root,
        storage_binding=storage_binding,
    )
    token = manager.acquire(launch)
    containment = manager.recovery_containment_for_token(token)
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True); time.sleep(120)"
    worker = containment.spawn([sys.executable, "-c", code], env=os.environ.copy())
    containment.attach(worker)
    containment.resume(worker)
    return manager, worker


def node_runtime(profile, worker_root, mode):
    assert NODE_TOKEN_ENV not in os.environ
    assert "--pause_sharing_on_start" in sys.argv and "--local_inference_cpu_only" in sys.argv
    assert os.environ["COMMUNITYAI_VOLUNTEER_PARENT_PID"] == str(os.getpid())
    durable = json.loads((profile / "anchor" / "state.json").read_text())
    assert durable["generation"]["pid"] == os.getpid()
    generation = durable["generation"]["id"]
    if mode.startswith("bootstrap_"):
        prepared = json.loads((profile / "anchor" / "bootstrap.json").read_text())
        assert prepared["ready"] and prepared["attempt"]["generation"] == generation
        assert (profile / "node" / "node-config.json").is_file()
        from drift.node.linux_anchor_entry import require_admitted_catalog_writer

        require_admitted_catalog_writer(profile / "node", profile / "node" / "node-config.json")
        if mode.startswith("bootstrap_writer_"):
            from contextlib import ExitStack

            from drift.node.catalog_bootstrap import (
                CatalogBootstrapConfig,
                CatalogBootstrapError,
                CatalogBootstrapInstaller,
                _catalog_bootstrap_lock,
            )
            from drift.node.config_lock import persistent_sidecar_lock
            from drift.node.policy_store import ContributionPolicyConflictError, ContributionPolicyStore
            from drift.node.worker_supervisor import WorkerSupervisor, WorkerSupervisorSettings

            config_path = profile / "node" / "node-config.json"
            config = json.loads(config_path.read_text())
            installer = CatalogBootstrapInstaller(
                CatalogBootstrapConfig.load(config["catalog_bootstrap_path"]),
                data_dir=profile / "node",
                config_path=config_path,
                fetch_text=lambda *_: Path(config["catalog_path"]).read_text(),
            )
            # Actual admitted child, actual original writer locks; no repair
            # is needed, so intact refresh has no catalog/config write effect.
            installer.repair_existing_config()
            installer.refresh()
            # Real policy persistence, with an empty worker-supervisor fixture:
            # this qualifies config writes, not model/worker execution.
            policy = ContributionPolicyStore(
                config_path, WorkerSupervisor(()), lambda _: WorkerSupervisorSettings(launches=(), stop_timeout=1)
            )
            snapshot = policy.snapshot()
            policy.update(
                dict(snapshot["policy"], max_processing_percent=73), expected_revision=snapshot["config_revision"]
            )
            assert json.loads(config_path.read_text())["contribution_policy"]["max_processing_percent"] == 73
            snapshot = policy.snapshot()
            before = config_path.read_bytes()
            _, _, kind, fault = mode.split("_")
            lock = (
                profile / "node" / (".catalog-bootstrap.lock" if kind == "catalog" else ".node-config.json.write.lock")
            )

            def lose_lock():
                lock.rename(lock.with_name(lock.name + ".retained"))
                if fault.endswith("replaced"):
                    lock.write_bytes(b"{}")
                    lock.chmod(0o600)

            original_fsync = os.fsync

            def lose_during_persistence(descriptor):
                original_fsync(descriptor)
                target = os.readlink("/proc/self/fd/" + str(descriptor))
                if ".node-config.json." in target and target.endswith(".tmp"):
                    lose_lock()

            results = []
            with ExitStack() as held:
                if fault.startswith("late-"):
                    # The real policy writer already holds its original FD
                    # when fsync returns; authority must be rechecked before exchange.
                    os.fsync = lose_during_persistence
                else:
                    authority = require_admitted_catalog_writer(profile / "node", config_path)
                    held.enter_context(
                        _catalog_bootstrap_lock(
                            profile / "node" / ".catalog-bootstrap.lock",
                            expected_identity=authority["node/.catalog-bootstrap.lock"],
                        )
                    )
                    held.enter_context(
                        persistent_sidecar_lock(
                            config_path, expected_identity=authority["node/.node-config.json.write.lock"]
                        )
                    )
                    lose_lock()
                try:
                    policy.update(
                        dict(snapshot["policy"], max_processing_percent=74),
                        expected_revision=snapshot["config_revision"],
                    )
                except ContributionPolicyConflictError:
                    results.append("refused")
                else:
                    raise AssertionError("lost writer lock authorized policy mutation")
                finally:
                    os.fsync = original_fsync
                for method in (installer.repair_existing_config, installer.refresh, installer.install):
                    try:
                        method()
                    except CatalogBootstrapError:
                        results.append("refused")
                    else:
                        raise AssertionError("lost writer lock authorized mutation")
            assert config_path.read_bytes() == before
            assert lock.exists() == fault.endswith("replaced")
            (profile / "writer-refusals.json").write_text(json.dumps(results))
    if mode == "api":
        from linux_anchor_api_fixture import serve

        try:
            return serve(profile, worker_root)
        except BaseException:
            import traceback

            (profile / "api-fixture-error.txt").write_text(traceback.format_exc())
            raise
    worker = manager = None
    if mode in ("entry_loss", "entry_replacement", "binding_loss", "binding_replacement"):
        from drift.node.resource_reservations import ResourceReservationError

        replacement = mode.endswith("replacement")
        if mode.startswith("entry_"):
            lose_journal(profile, replacement)
        try:
            manager, worker = spawn_worker(
                profile, worker_root, lose_after_binding=replacement if mode.startswith("binding_") else None
            )
        except (RecoverableStateError, ResourceReservationError):
            (profile / "admission-refused").touch()
        else:
            raise AssertionError("lost admission evidence was reset")
    if mode in (
        "worker",
        "worker_crash",
        "worker_poison",
        "missing_worker_journal",
        "combined_loss",
        "replaced_journal",
        "poll_failure",
        "wait_failure",
        "loop_poll_failure",
        "leaf_replaced",
        "unknown_sibling",
    ):
        manager, worker = spawn_worker(profile, worker_root)
    if mode in ("missing_journal", "missing_worker_journal", "combined_loss"):
        (profile / "node" / "resource-reservations" / "generations.json").unlink()
    if mode == "combined_loss":
        (profile / "node" / "resource-reservations" / "admission.lock").unlink()
    if mode == "replaced_journal":
        journal = profile / "node" / "resource-reservations"
        journal.rename(profile / "node" / "retained-journal")
        journal.mkdir(mode=0o700)
    if mode == "leaf_replaced":
        leaf = Path(durable["generation"]["cgroup"]["root"])
        # Deliberately hostile fixture migration within this test's owned nodes
        # root; production never migrates a launched node to repair evidence.
        displaced = leaf.with_name(leaf.name + "-displaced")
        displaced.mkdir()
        (displaced / "cgroup.procs").write_text(str(os.getpid()))
        leaf.rmdir()
        leaf.mkdir(mode=0o700)
    if mode == "unknown_sibling":
        leaf = Path(worker_root).parent / "nodes" / "unknown"
        leaf.mkdir()
        descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY)
        try:
            unknown = native.spawn(
                descriptor, [sys.executable, "-c", "import time;time.sleep(120)"], env=os.environ.copy()
            )
            unknown.resume()
        finally:
            os.close(descriptor)
    descendant = None
    if mode in ("descendant", "crash"):
        descendant = os.fork()
        if descendant == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                time.sleep(0.05)
    (profile / "entered.json").write_text(
        json.dumps(
            dict(
                pid=os.getpid(),
                generation=generation,
                phase=durable["phase"],
                descendant=descendant,
                worker=None if worker is None else worker.pid,
                group=Path("/proc/self/cgroup").read_text().strip(),
            )
        )
    )
    while True:
        if mode in ("crash", "worker_crash") and (profile / "crash-now").exists():
            os._exit(19)
        time.sleep(0.02)


def service(root, directory, mode):
    group = Path("/proc/self/cgroup").read_text()[3:].strip()
    anchor._query_properties = lambda: properties(os.getpid(), group)
    anchor._runtime_directory = lambda: directory
    profile = directory / "profile"
    profile.mkdir(mode=0o700)

    def launch(worker_root):
        if mode == "failed_start_barrier":
            raise OSError("fixture launch failure")
        return (
            [sys.executable, str(Path(__file__).resolve()), "node", str(profile), group, worker_root, mode],
            os.environ.copy(),
            str(profile),
        )

    original_spawn = native.spawn
    if mode in ("birth_barrier", "exec_barrier"):

        def stalled_spawn(*args, **kwargs):
            child = original_spawn(*args, **kwargs)
            if mode == "birth_barrier":
                (profile / "barrier").touch()
                while not (profile / "release").exists():
                    time.sleep(0.01)
            else:
                original_exec = child.await_exec

                def stalled_exec(**kwargs):
                    (profile / "barrier").touch()
                    while not (profile / "release").exists():
                        time.sleep(0.01)
                    return original_exec(**kwargs)

                child.await_exec = stalled_exec
            return child

        native.spawn = stalled_spawn
    if mode in ("write_failure", "worker_poison"):
        from drift.node import linux_anchor_state

        publish = linux_anchor_state.private._replace

        def uncertain(path, value):
            publish(path, value)
            if (
                mode == "write_failure"
                and value.get("generation") is not None
                and value["generation"]["pid"] is not None
            ) or (mode == "worker_poison" and value["operation"] == "drain"):
                raise OSError("fixture lost durable write acknowledgement")

        linux_anchor_state.private._replace = uncertain

    def barrier():
        (directory / "barrier").touch()
        while not (directory / "release").exists():
            time.sleep(0.01)

    if mode == "dequeue_barrier":
        original_start = AnchorNode._start

        def stalled_start(self, request_id):
            barrier()
            return original_start(self, request_id)

        AnchorNode._start = stalled_start
    if mode == "publish_barrier":
        original_publish = AnchorNode._publish

        def stalled_publish(self, **kwargs):
            if self._state.value["phase"] == "starting" and not (directory / "release").exists():
                barrier()
            return original_publish(self, **kwargs)

        AnchorNode._publish = stalled_publish
    if mode == "drain_barrier":
        original_cleanup = AnchorNode._cleanup

        def stalled_cleanup(self, **kwargs):
            if self._active is not None and self._active[0] == "drain":
                barrier()
            return original_cleanup(self, **kwargs)

        AnchorNode._cleanup = stalled_cleanup
    if mode == "failed_start_barrier":
        original_write = AnchorNode._write

        def stalled_write(self, **changes):
            if changes.get("phase") == "blocked":
                barrier()
            return original_write(self, **changes)

        AnchorNode._write = stalled_write
    if mode == "close_barrier":
        original_close = AnchorNode._close

        def stalled_close(self, timeout):
            if self.finished.is_set() and not self._closed:
                barrier()
            return original_close(self, timeout)

        AnchorNode._close = stalled_close

    def handle_error(*args, **kwargs):
        raise OSError("fixture direct-handle failure")

    if mode in ("poll_failure", "wait_failure"):
        original_cleanup = AnchorNode._cleanup

        def failed_handle_cleanup(self, **kwargs):
            if self._process is not None:
                setattr(self._process, "poll" if mode == "poll_failure" else "wait", handle_error)
            return original_cleanup(self, **kwargs)

        AnchorNode._cleanup = failed_handle_cleanup
    if mode in ("loop_poll_failure", "retained_entry"):
        original_start = AnchorNode._start

        def affected_start(self, request_id):
            if mode == "retained_entry":
                self._fixture_foreign_manager, worker = spawn_worker(profile, self.layout.profiles[3].root)
                self._kill_profile(self.layout.profiles[3])
                worker.wait(timeout=5)
                worker.stdout.close()
            original_start(self, request_id)
            if mode == "loop_poll_failure":
                deadline = time.monotonic() + 8
                while not (profile / "entered.json").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                self._process.poll = handle_error

        AnchorNode._start = affected_start

    original_channel = anchor.AnchorChannel

    def ready_channel(*args):
        channel = original_channel(*args)
        print(json.dumps(dict(pid=os.getpid(), group=group)), flush=True)
        return channel

    anchor.AnchorChannel = ready_channel
    original_shutdown = AnchorNode.request_shutdown

    def observed_shutdown(self):
        original_shutdown(self)
        (directory / "shutdown-requested").touch()

    AnchorNode.request_shutdown = observed_shutdown
    done = threading.Event()

    def stop_watcher():
        while not done.wait(0.02):
            if (directory / "stop").exists():
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=stop_watcher, daemon=True).start()
    preparation = None
    if mode.startswith("bootstrap_"):
        from communityai_desktop.credentials import CredentialMissingError
        from communityai_desktop.profiles import VolunteerProfile

        from drift.node.linux_anchor_bootstrap import AnchorBootstrap, build_bootstrap_plan
        from drift.utils.auto_config import _CLASS_MAPPING

        # This standalone driver omits drift's heavyweight model registration.
        # Capability metadata is a fixture; the separate plan suite tests the
        # actual registered runtime validator and no models execute here.
        _CLASS_MAPPING.setdefault("llama", {})

        fixed_profile = VolunteerProfile(profile)
        plan = build_bootstrap_plan(directory / "bundle", fixed_profile, initialize=True)

        class KeyringFixture:
            service, account = fixed_profile.credential_service, fixed_profile.credential_account
            secret = None
            sets = 0

            def get(self):
                if mode == "bootstrap_locked" and not (directory / "keyring-unlocked").exists():
                    raise OSError("fixture locked before first set")
                if mode == "bootstrap_ready_locked" and (directory / "keyring-locked").exists():
                    raise OSError("fixture locked ready keyring")
                if self.sets and mode == "bootstrap_unknown":
                    raise OSError("fixture cannot establish keyring outcome")
                if self.secret is None:
                    raise CredentialMissingError()
                return self.secret

            def set(self, secret):
                self.secret = secret
                self.sets += 1
                (directory / "keyring-sets").write_text(str(self.sets))
                if mode in {"bootstrap_key_barrier", "bootstrap_unknown"}:
                    barrier()

        preparation = AnchorBootstrap(plan, fixed_profile, KeyringFixture())
        if mode == "bootstrap_config_barrier":
            commit = preparation._commit

            def held_commit(index):
                commit(index)
                if index == len(plan.outputs) - 1 and not (directory / "release").exists():
                    barrier()

            preparation._commit = held_commit
    try:
        return anchor.serve_anchor(
            controller_factory=lambda layout: AnchorNode(
                layout, profile, launch, initialize=True, bootstrap=preparation
            )
        )
    finally:
        done.set()


if __name__ != "__main__":
    pytestmark = pytest.mark.skipif(
        not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
        reason="requires private native cgroup fixture",
    )

    @pytest.fixture
    def running_node(tmp_path, monkeypatch):
        root = Path(os.environ["COMMUNITYAI_TEST_CGROUP_ROOT"]) / ("node-anchor-" + uuid4().hex)
        root.mkdir(mode=0o700)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        children = []

        def start(mode="normal"):
            if mode.startswith("bootstrap_"):
                from test_catalog_publication import _documents

                from drift.catalog_release import write_catalog_publication_bundle

                bootstrap, envelope, manifests = _documents()
                write_catalog_publication_bundle(tmp_path / "bundle", bootstrap, envelope, manifests)
            process = native.spawn(
                descriptor,
                [sys.executable, str(Path(__file__).resolve()), "anchor", str(root), str(tmp_path), mode],
                env=os.environ.copy(),
            )
            children.append(process)
            process.resume()
            assert select.select([process.stdout], [], [], 10)[0], "service did not report"
            line = process.stdout.readline()
            assert line.startswith("{"), line + process.stdout.read()
            report = json.loads(line)
            monkeypatch.setattr(anchor, "_query_properties", lambda: properties(report["pid"], report["group"]))
            monkeypatch.setattr(anchor, "_runtime_directory", lambda: tmp_path)
            return process

        yield root, tmp_path, start
        (root / "cgroup.kill").write_text("1")
        for process in children:
            process.wait(timeout=8)
            process.stdout.close()
        deadline = time.monotonic() + 5
        while "populated 1" in (root / "cgroup.events").read_text():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
        os.close(descriptor)
        root.rmdir()

    def observe():
        return control.control_anchor()["node"]

    def until(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while True:
            value = observe()
            if predicate(value):
                return value
            assert time.monotonic() < deadline, json.dumps(value, sort_keys=True)
            time.sleep(0.02)

    def condition(value):
        generation = value["generation"]
        return dict(
            generation=None if generation is None else generation["id"], pending_request_id=value["pending_request_id"]
        )

    def command(operation, request_id=None):
        request_id = request_id or uuid4().hex
        deadline = time.monotonic() + 5
        while True:
            value = observe()
            try:
                result = control.control_anchor(
                    operation, revision=value["revision"], request_id=request_id, condition=condition(value)
                )
                return result["node"], request_id
            except RecoverableStateError:
                assert time.monotonic() < deadline, value

    def wait_file(path, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists():
            assert time.monotonic() < deadline, path
            time.sleep(0.02)

    def test_start_durable_child_duplicate_request_whole_tree_drain_and_restart(running_node):
        root, directory, start = running_node
        process = start("descendant")
        assert until(lambda s: s["drain_complete"])["phase"] == "idle"
        assert anchor.inspect_anchor()["node_generation"] is None
        _, request_id = command("start")
        running = until(lambda s: s["phase"] == "running")
        wait_file(directory / "profile" / "entered.json")
        entered = json.loads((directory / "profile" / "entered.json").read_text())
        assert entered["generation"] == running["generation"]["id"]
        assert entered["pid"] == running["generation"]["pid"]
        assert entered["phase"] in ("starting", "running")
        assert not running["api_ready"] and not running["maintenance"]
        same = control.control_anchor(
            "start", revision=0, request_id=request_id, condition=dict(generation=None, pending_request_id=None)
        )["node"]
        assert same["generation"] == running["generation"]
        with pytest.raises(RecoverableStateError):
            control.control_anchor(
                "start", revision=running["revision"], request_id=uuid4().hex, condition=condition(running)
            )
        command("drain")
        idle = until(lambda s: s["drain_complete"])
        assert idle["request_id"] != request_id
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()
        assert (
            json.loads((directory / "profile" / "node" / "resource-reservations" / "generations.json").read_text())[
                "reservations"
            ]
            == []
        )
        command("start")
        second = until(lambda s: s["phase"] == "running")
        assert second["generation"]["id"] != running["generation"]["id"]
        command("drain")
        until(lambda s: s["drain_complete"])
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 0

    def test_signed_bootstrap_is_ready_before_real_native_node_birth_and_restart(running_node):
        root, directory, start = running_node
        process = start("bootstrap_normal")
        until(lambda s: s["drain_complete"])
        _, request = command("start")
        first = until(lambda s: s["phase"] == "running")
        wait_file(directory / "profile" / "entered.json")
        marker = json.loads((directory / "profile" / "anchor" / "bootstrap.json").read_text())
        assert marker["ready"] and marker["attempt"] == dict(request_id=request, generation=first["generation"]["id"])
        assert (directory / "keyring-sets").read_text() == "1"
        command("drain")
        until(lambda s: s["drain_complete"])
        command("start")
        second = until(lambda s: s["phase"] == "running")
        assert second["generation"]["id"] != first["generation"]["id"]
        assert (directory / "keyring-sets").read_text() == "1"
        command("drain")
        until(lambda s: s["drain_complete"])
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 0

    @pytest.mark.parametrize("kind", ["catalog", "config"])
    @pytest.mark.parametrize("fault", ["missing", "replaced", "late-missing", "late-replaced"])
    def test_real_admitted_catalog_writer_refuses_lost_pinned_lock(running_node, kind, fault):
        root, directory, start = running_node
        process = start("bootstrap_writer_" + kind + "_" + fault)
        until(lambda s: s["drain_complete"])
        command("start")
        path = directory / "profile" / "writer-refusals.json"
        # This fixture imports the full catalog/policy stack in a fresh child.
        # Allow cold imports under parallel test load, not a production UX SLA.
        wait_file(path, timeout=30)
        assert json.loads(path.read_text()) == ["refused"] * 4
        (directory / "stop").touch()
        assert process.wait(timeout=8) != 0  # Lost evidence cannot be clean exit.

    def test_ready_locked_keyring_retries_same_controller_without_new_set(running_node):
        root, directory, start = running_node
        process = start("bootstrap_ready_locked")
        until(lambda s: s["drain_complete"])
        command("start")
        until(lambda s: s["phase"] == "running")
        wait_file(directory / "profile" / "entered.json")
        command("drain")
        until(lambda s: s["drain_complete"])
        (directory / "keyring-locked").touch()
        _, request = command("start")
        idle = until(lambda s: s["drain_complete"] and s["request_id"] == request)
        assert idle["phase"] == "idle" and process.poll() is None
        (directory / "keyring-locked").unlink()
        command("start")
        until(lambda s: s["phase"] == "running")
        assert (directory / "keyring-sets").read_text() == "1"
        command("drain")
        until(lambda s: s["drain_complete"])
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 0

    @pytest.mark.parametrize("mode", ["bootstrap_key_barrier", "bootstrap_config_barrier"])
    @pytest.mark.parametrize("stop", ["drain", "sigterm"])
    def test_bootstrap_cancel_keeps_exclusion_and_no_child_birth_until_reconciled(running_node, mode, stop):
        root, directory, start = running_node
        process = start(mode)
        until(lambda s: s["drain_complete"])
        _, request = command("start")
        wait_file(directory / "barrier")
        planned = observe()
        assert planned["phase"] == "starting" and planned["generation"]["pid"] is None
        assert planned["api_identity"] is None and not planned["drain_complete"]
        marker = json.loads((directory / "profile" / "anchor" / "bootstrap.json").read_text())
        assert marker["attempt"]["request_id"] == request
        assert not any(path.is_dir() for path in (root / "nodes").iterdir())
        import fcntl

        for lock in (
            "anchor-state.lock",
            "node-lifetime.lock",
            "node/resource-reservations/admission.lock",
            "node/.catalog-bootstrap.lock",
            "node/.node-config.json.write.lock",
        ):
            descriptor = os.open(directory / "profile" / lock, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        if stop == "drain":
            before = time.monotonic()
            command("drain")
            assert time.monotonic() - before < 2
            assert not observe()["drain_complete"]
        else:
            os.kill(process.pid, signal.SIGTERM)
            wait_file(directory / "shutdown-requested")
        assert process.poll() is None
        assert not (directory / "profile" / "entered.json").exists()
        (directory / "release").touch()
        if stop == "sigterm":
            assert process.wait(timeout=8) == 0
        else:
            until(lambda s: s["drain_complete"])
            assert not (directory / "profile" / "entered.json").exists()
            command("start")
            until(lambda s: s["phase"] == "running")
            wait_file(directory / "profile" / "entered.json")
            command("drain")
            until(lambda s: s["drain_complete"])
            (directory / "stop").touch()
            assert process.wait(timeout=8) == 0
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()
        assert "populated 0" in (root / "workers" / "cgroup.events").read_text()
        assert (directory / "keyring-sets").read_text() == "1"

    def test_unknown_keyring_result_blocks_drain_and_never_claims_clean_exit(running_node):
        root, directory, start = running_node
        process = start("bootstrap_unknown")
        until(lambda s: s["drain_complete"])
        command("start")
        wait_file(directory / "barrier")
        command("drain")
        (directory / "release").touch()
        blocked = until(lambda s: s["phase"] == "blocked" and s["pending_request_id"] is None)
        assert not blocked["drain_complete"]
        assert not (directory / "profile" / "entered.json").exists()
        assert not (directory / "profile" / "node" / "node-config.json").exists()
        assert (directory / "keyring-sets").read_text() == "1"
        (directory / "stop").touch()
        assert process.wait(timeout=8) != 0

    def test_locked_keyring_before_set_retries_without_replacing_anchor_or_journal(running_node):
        root, directory, start = running_node
        process = start("bootstrap_locked")
        until(lambda s: s["drain_complete"])
        _, request = command("start")
        idle = until(lambda s: s["drain_complete"] and s["request_id"] == request)
        assert idle["phase"] == "idle" and idle["operation"] == "start"
        assert not (directory / "keyring-sets").exists()
        assert not (directory / "profile" / "entered.json").exists()
        marker_path = directory / "profile" / "anchor" / "bootstrap.json"
        transaction = json.loads(marker_path.read_text())["transaction"]
        (directory / "keyring-unlocked").touch()
        command("start")
        until(lambda s: s["phase"] == "running")
        wait_file(directory / "profile" / "entered.json")
        assert json.loads(marker_path.read_text())["transaction"] == transaction
        assert (directory / "keyring-sets").read_text() == "1"
        command("drain")
        until(lambda s: s["drain_complete"])
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 0

    @pytest.mark.parametrize("mode", ["birth_barrier", "exec_barrier"])
    def test_cancel_during_native_barrier_keeps_control_responsive_and_no_late_running(running_node, mode):
        root, directory, start = running_node
        start(mode)
        until(lambda s: s["drain_complete"])
        command("start")
        profile = directory / "profile"
        wait_file(profile / "barrier")
        before = time.monotonic()
        value = observe()
        command("drain")
        assert time.monotonic() - before < 2
        assert not value["drain_complete"] and value["phase"] == "starting"
        if mode == "birth_barrier":
            assert not (profile / "entered.json").exists()
        (profile / "release").touch()
        until(lambda s: s["drain_complete"])
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()
        if mode == "birth_barrier":
            assert not (profile / "entered.json").exists()

    def test_crashed_node_descendant_cannot_survive_clean_ack(running_node):
        root, directory, start = running_node
        start("crash")
        until(lambda s: s["drain_complete"])
        command("start")
        until(lambda s: s["phase"] == "running")
        profile = directory / "profile"
        wait_file(profile / "entered.json")
        assert json.loads((profile / "entered.json").read_text())["descendant"] > 1
        (profile / "crash-now").touch()
        until(lambda s: s["drain_complete"])
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()

    @pytest.mark.parametrize("mode", ["worker", "worker_crash"])
    def test_node_death_recovers_real_foreign_worker_journal_before_clean(running_node, mode):
        root, directory, start = running_node
        process = start(mode)
        until(lambda s: s["drain_complete"])
        command("start")
        until(lambda s: s["phase"] == "running")
        profile = directory / "profile"
        wait_file(profile / "entered.json")
        assert json.loads((profile / "entered.json").read_text())["worker"] > 1
        journal = profile / "node" / "resource-reservations" / "generations.json"
        assert len(json.loads(journal.read_text())["reservations"]) == 1
        assert "populated 1" in (root / "workers" / "cgroup.events").read_text()
        if mode == "worker_crash":
            (profile / "crash-now").touch()
        else:
            command("drain")
        until(lambda s: s["drain_complete"], timeout=15)
        assert json.loads(journal.read_text())["reservations"] == []
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()
        assert "populated 0" in (root / "workers" / "cgroup.events").read_text()
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 0

    def test_lost_journal_denies_clean_drain_and_zero_service_exit(running_node):
        root, directory, start = running_node
        process = start("missing_journal")
        until(lambda s: s["drain_complete"])
        command("start")
        wait_file(directory / "profile" / "entered.json")
        command("drain")
        blocked = until(lambda s: s["phase"] == "blocked")
        assert not blocked["drain_complete"] and not blocked["maintenance"]
        assert not (directory / "profile" / "node" / "resource-reservations" / "generations.json").exists()
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 75

    @pytest.mark.parametrize("mode", ["forged_token", "wrong_root", "forged_lifetime"])
    def test_real_linux_launcher_refuses_forged_entry_before_model_body(running_node, mode):
        root, directory, start = running_node
        start(mode)
        until(lambda s: s["drain_complete"])
        command("start")
        stopped = until(
            lambda s: s["generation"] is not None
            and s["pending_request_id"] is None
            and s["phase"] in ("idle", "blocked"),
            # Fault containment may make two bounded cleanup attempts. Do not
            # confuse a loaded host's scheduling delay with a successful ack.
            timeout=20,
        )
        assert not stopped["api_ready"]
        assert not (directory / "profile" / "entered.json").exists()
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()

    def test_uncertain_birth_record_blocks_and_never_executes_child(running_node):
        root, directory, start = running_node
        start("write_failure")
        until(lambda s: s["drain_complete"])
        command("start")
        blocked = until(lambda s: s["phase"] == "blocked")
        assert not blocked["drain_complete"] and not blocked["maintenance"]
        assert not (directory / "profile" / "entered.json").exists()
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()
        with pytest.raises(RecoverableStateError):
            control.control_anchor(
                "start", revision=blocked["revision"], request_id=uuid4().hex, condition=condition(blocked)
            )

    def test_stale_unknown_or_path_injected_protocol_never_changes_generation(running_node):
        root, directory, start = running_node
        start()
        idle = until(lambda s: s["drain_complete"])
        bad = control.request("start", "a" * 64, idle["revision"], uuid4().hex, condition(idle))
        bad["argv"] = ["/private/command"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(directory / "communityai-multigpu-anchor" / "control.sock"))
            connection.sendall(anchor._encode(bad))
            connection.shutdown(socket.SHUT_WR)
            assert connection.recv(1) == b""
        with pytest.raises(RecoverableStateError):
            control.control_anchor(
                "start", revision=idle["revision"] - 1, request_id=uuid4().hex, condition=condition(idle)
            )
        assert observe()["generation"] is None

    @pytest.mark.parametrize("mode", ["dequeue_barrier", "publish_barrier"])
    def test_published_cas_allows_only_one_cancelling_drain_while_start_active(running_node, mode):
        root, directory, start = running_node
        start(mode)
        initial = until(lambda s: s["drain_complete"])
        _, start_id = command("start")
        wait_file(directory / "barrier")
        value = observe()
        assert value["revision"] == initial["revision"]
        assert value["pending_request_id"] == start_id and not value["drain_complete"]
        drain_id = uuid4().hex
        accepted = control.control_anchor(
            "drain", revision=value["revision"], request_id=drain_id, condition=condition(value)
        )["node"]
        assert accepted["pending_request_id"] == drain_id and not accepted["drain_complete"]
        control.control_anchor("drain", revision=value["revision"], request_id=drain_id, condition=condition(value))
        for operation in ("start", "drain"):
            with pytest.raises(RecoverableStateError):
                control.control_anchor(
                    operation, revision=value["revision"], request_id=uuid4().hex, condition=condition(value)
                )
        (directory / "release").touch()
        until(lambda s: s["drain_complete"])
        assert not (directory / "profile" / "entered.json").exists()
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()

    def test_active_drain_is_not_dequeued_into_another_command_window(running_node):
        root, directory, start = running_node
        start("drain_barrier")
        until(lambda s: s["drain_complete"])
        _, request_id = command("drain")
        wait_file(directory / "barrier")
        value = observe()
        assert value["pending_request_id"] == request_id and not value["drain_complete"]
        control.control_anchor("drain", revision=value["revision"], request_id=request_id, condition=condition(value))
        for operation in ("start", "drain"):
            with pytest.raises(RecoverableStateError):
                control.control_anchor(
                    operation, revision=value["revision"], request_id=uuid4().hex, condition=condition(value)
                )
        (directory / "release").touch()
        until(lambda s: s["drain_complete"])

    def test_failed_start_never_publishes_interim_completed_drain(running_node):
        root, directory, start = running_node
        start("failed_start_barrier")
        until(lambda s: s["drain_complete"])
        command("start")
        wait_file(directory / "barrier")
        value = observe()
        assert not value["drain_complete"] and value["pending_request_id"] is not None
        with pytest.raises(RecoverableStateError):
            control.control_anchor(
                "start", revision=value["revision"], request_id=uuid4().hex, condition=condition(value)
            )
        (directory / "release").touch()
        blocked = until(lambda s: s["phase"] == "blocked" and s["pending_request_id"] is None)
        assert not blocked["drain_complete"]

    @pytest.mark.parametrize("mode", ["worker_poison", "missing_worker_journal", "combined_loss", "replaced_journal"])
    def test_state_or_journal_loss_stops_real_worker_without_false_completion(running_node, mode):
        root, directory, start = running_node
        process = start(mode)
        until(lambda s: s["drain_complete"])
        command("start")
        wait_file(directory / "profile" / "entered.json")
        assert "populated 1" in (root / "workers" / "cgroup.events").read_text()
        command("drain")
        value = until(lambda s: s["phase"] == "blocked" and s["pending_request_id"] is None, timeout=15)
        assert not value["drain_complete"]
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()
        assert "populated 0" in (root / "workers" / "cgroup.events").read_text()
        journal = directory / "profile" / "node" / "resource-reservations"
        if mode in ("combined_loss", "replaced_journal"):
            assert not (journal / "admission.lock").exists()
            assert not (journal / "generations.json").exists()
        if mode == "worker_poison":
            assert json.loads((journal / "generations.json").read_text())["reservations"] == []
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 75

    @pytest.mark.parametrize(
        "target",
        [
            "node-lifetime.lock",
            "anchor-state.lock",
            "anchor/state.json",
            "anchor/resources.json",
            "node/resource-reservations",
            "node-leaf",
        ],
    )
    def test_final_close_revalidates_identity_after_completed_cleanup(running_node, target):
        root, directory, start = running_node
        process = start("close_barrier")
        until(lambda s: s["drain_complete"])
        if target == "node-leaf":
            command("start")
            until(lambda s: s["phase"] == "running")
            command("drain")
            until(lambda s: s["drain_complete"])
        (directory / "stop").touch()
        wait_file(directory / "barrier")
        path = (
            next(p for p in (root / "nodes").iterdir() if p.is_dir())
            if target == "node-leaf"
            else directory / "profile" / target
        )
        if target == "node-leaf":
            path.rmdir()
            path.mkdir()
        else:
            path.rename(path.with_name(path.name + ".retained"))
        (directory / "release").touch()
        assert process.wait(timeout=8) == 75

    @pytest.mark.parametrize(
        "mode", ["poll_failure", "wait_failure", "loop_poll_failure", "leaf_replaced", "unknown_sibling"]
    )
    def test_direct_handle_or_leaf_fault_cannot_bypass_either_root_stop(running_node, mode):
        root, directory, start = running_node
        process = start(mode)
        until(lambda s: s["drain_complete"])
        command("start")
        wait_file(directory / "profile" / "entered.json")
        entered = json.loads((directory / "profile" / "entered.json").read_text())
        assert entered["worker"] > 1
        if mode != "loop_poll_failure":
            command("drain")
        if mode == "unknown_sibling":
            until(lambda s: s["drain_complete"])
        else:
            value = until(lambda s: s["phase"] == "blocked" and s["pending_request_id"] is None, timeout=15)
            assert not value["drain_complete"]
        for name in ("nodes", "workers"):
            assert "populated 0" in (root / name / "cgroup.events").read_text()
        (directory / "stop").touch()
        assert process.wait(timeout=8) == (0 if mode == "unknown_sibling" else 75)

    def test_start_rechecks_foreign_journal_under_guard_before_birth(running_node):
        root, directory, start = running_node
        start("retained_entry")
        until(lambda s: s["drain_complete"])
        command("start")
        value = until(lambda s: s["phase"] == "blocked" and s["pending_request_id"] is None, timeout=15)
        assert value["generation"] is None and not value["drain_complete"]
        assert not (directory / "profile" / "entered.json").exists()
        journal = directory / "profile" / "node" / "resource-reservations" / "generations.json"
        assert len(json.loads(journal.read_text())["reservations"]) == 1
        for name in ("nodes", "workers"):
            assert "populated 0" in (root / name / "cgroup.events").read_text()

    @pytest.mark.parametrize("mode", ["worker", "birth_barrier", "exec_barrier"])
    def test_direct_service_sigterm_drains_live_work_or_cancelled_birth(running_node, mode):
        root, directory, start = running_node
        process = start(mode)
        until(lambda s: s["drain_complete"])
        command("start")
        profile = directory / "profile"
        wait_file(profile / ("entered.json" if mode == "worker" else "barrier"))
        (directory / "stop").touch()
        wait_file(directory / "shutdown-requested")
        if mode != "worker":
            (profile / "release").touch()
        assert process.wait(timeout=15) == 0
        for name in ("nodes", "workers"):
            assert "populated 0" in (root / name / "cgroup.events").read_text()
        journal = profile / "node" / "resource-reservations" / "generations.json"
        assert json.loads(journal.read_text())["reservations"] == []
        if mode == "birth_barrier":
            assert not (profile / "entered.json").exists()

    @pytest.mark.parametrize("mode", ["entry_loss", "entry_replacement", "binding_loss", "binding_replacement"])
    def test_child_never_resets_evidence_lost_after_entry_before_first_admission(running_node, mode):
        root, directory, start = running_node
        start(mode)
        until(lambda s: s["drain_complete"])
        command("start")
        profile = directory / "profile"
        wait_file(profile / "admission-refused")
        journal = profile / "node" / "resource-reservations"
        assert not (journal / "admission.lock").exists()
        assert not (journal / "generations.json").exists()
        assert "populated 0" in (root / "workers" / "cgroup.events").read_text()
        command("drain")
        value = until(lambda s: s["phase"] == "blocked" and s["pending_request_id"] is None)
        assert not value["drain_complete"]
        assert "populated 0" in (root / "nodes" / "cgroup.events").read_text()

    @pytest.mark.parametrize("retry", [False, True])
    def test_frozen_worker_root_cannot_veto_stop_but_withholds_completion(running_node, retry):
        root, directory, start = running_node
        process = start("worker")
        until(lambda s: s["drain_complete"])
        command("start")
        wait_file(directory / "profile" / "entered.json")
        freezer = root / "workers" / "cgroup.freeze"
        freezer.write_text("1")
        deadline = time.monotonic() + 5
        while "frozen 1" not in (root / "workers" / "cgroup.events").read_text():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not observe()["drain_complete"]
        command("drain")
        value = until(lambda s: s["phase"] == "blocked" and s["pending_request_id"] is None, timeout=15)
        assert not value["drain_complete"]
        for name in ("nodes", "workers"):
            assert "populated 0" in (root / name / "cgroup.events").read_text()
        if retry:
            freezer.write_text("0")
            command("drain")
            until(lambda s: s["drain_complete"])
        (directory / "stop").touch()
        assert process.wait(timeout=10) == (0 if retry else 75)


if __name__ == "__main__":
    if sys.argv[1] == "node":
        body(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])
    else:
        raise SystemExit(service(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]))
