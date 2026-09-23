"""Native Linux supervisor -> managed CLI -> private loading -> durable cleanup.

Only the model body, resource snapshot and builder GPU inventory are controlled.
No model, GPU, network or download executes. The native runner supplies a private
delegation and compiled extension; this does not qualify an installed desktop.
"""

import inspect
import json
import os
import select
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_managed_placement_sizing import managed_candidates
from test_managed_resource_runtime import managed_launch

ROOT = os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT")
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not ROOT,
    reason="requires explicit private Linux cgroup delegation and compiled native backend",
)


@pytest.fixture(autouse=True)
def isolated_worker_root(monkeypatch):
    """Keep the driver outside the worker subtree required to drain globally.

    The opt-in runner lives in the delegated parent. Treating that populated
    parent as the worker root makes a correct empty-tree check always refuse.
    No production check is mocked or weakened by separating these lifetimes.
    """
    from drift.node.linux_cgroup_recovery import verify_cgroup_tree_empty

    root = Path(ROOT) / ("supervisor-workers-" + uuid4().hex)
    root.mkdir(mode=0o700)
    monkeypatch.setitem(globals(), "ROOT", str(root))
    assert (root / "cgroup.procs").read_text() == ""
    yield
    # Refuse teardown pruning if real subtree death cannot be proved.
    verify_cgroup_tree_empty(root)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
    root.rmdir()


def resource_snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
    from drift.node.placement_resources import CacheSnapshot, ResourceSnapshot, VolumeSnapshot

    return ResourceSnapshot(
        now,
        host_limit_bytes,
        100 * 1024**3,
        tuple(CacheSnapshot(root, "disk", 0, limit) for root, limit in cache_limits.items()),
        (VolumeSnapshot("disk", 100 * 1024**3),),
    )


def wait_for(predicate, *, supervisor=None, seconds=60):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            status = None if supervisor is None else supervisor.snapshot("gpu-0")
            raise AssertionError("native connected runtime timed out: " + repr(status))
        time.sleep(0.02)


def entries(private):
    return json.loads((Path(private) / "generations.json").read_text(encoding="utf-8"))["reservations"]


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def child_overlay(directory, private):
    """Change only the child server body; leave its CLI and loading protocol real."""
    directory = Path(directory)
    directory.mkdir()
    (directory / "sitecustomize.py").write_text(
        "import json,os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "from drift.cli import run_server\n"
        f"CONTROL=Path({str(directory)!r})\n"
        f"PRIVATE=Path({str(private)!r})\n"
        "def publish(name,value):\n"
        "    p=CONTROL/name;t=p.with_suffix('.tmp');t.write_text(json.dumps(value));os.replace(t,p)\n"
        "class ControlledServer:\n"
        "    converted_model_name_or_path='controlled-native-body'\n"
        "    def __init__(self,args):\n"
        "        self.session=args['managed_loading_session']\n"
        "        token=self.session.binding.token\n"
        "        records=json.loads((PRIVATE/'generations.json').read_text())['reservations']\n"
        "        matches=[r for r in records if r['claim']['reservation_id']==token]\n"
        "        if len(matches)!=1 or matches[0]['recovery']['contract']!='linux_cgroup_v1':\n"
        "            raise RuntimeError('CLI body preceded durable native admission')\n"
        "        publish('constructed.json',dict(pid=os.getpid(),token=token,argv=sys.argv))\n"
        "    def run(self):\n"
        "        while not (CONTROL/'allow-ready').exists():time.sleep(.02)\n"
        "        child=subprocess.Popen([sys.executable,'-S','-c','import time;time.sleep(120)'],start_new_session=True)\n"
        "        publish('descendant.json',dict(worker=os.getpid(),grandchild=child.pid))\n"
        "        self.session.ready()\n"
        "        self._managed_ready=True\n"
        "        time.sleep(120)\n"
        "run_server.server_from_args=ControlledServer\n"
        "run_server.serve=lambda server,model:server.run()\n",
        encoding="utf-8",
    )
    return directory


def child_environment(overlay):
    return (
        ("PYTHONPATH", os.pathsep.join((str(overlay), os.environ.get("PYTHONPATH", "")))),
        ("HF_HUB_OFFLINE", "1"),
        ("TRANSFORMERS_OFFLINE", "1"),
        ("OMP_NUM_THREADS", "1"),
        ("MKL_NUM_THREADS", "1"),
        ("TOKENIZERS_PARALLELISM", "false"),
    )


def new_manager(private, root):
    from drift.node.resource_reservations import ResourceReservationManager

    return ResourceReservationManager(
        private,
        snapshot_provider=resource_snapshot,
        loading_protocol=True,
        recovery_protocol=True,
        worker_cgroup_root=root,
    )


def direct_launch(cache, manifest, overlay):
    """Controlled structural claim; the real node builder is exercised separately."""
    from drift.node.host_resources import canonical_cache_root
    from drift.node.placement_resources import ArtifactClaim, WorkerResourceClaim
    from drift.node.worker_supervisor import WorkerLaunch

    cache = canonical_cache_root(cache)
    manifest_digest, artifact_digest = "sha256:" + "b" * 64, "c" * 64
    command = (
        sys.executable,
        "-m",
        "drift.cli",
        "server",
        "--model_manifest",
        str(manifest),
        "--block_indices",
        "0:1",
        "--expected_manifest_digest",
        manifest_digest,
        "--expected_block_indices",
        "0:1",
        "--expected_artifact_bytes",
        "11",
        "--expected_artifact_set_digest",
        artifact_digest,
        "--cache_dir",
        cache,
        "--expected_cache_root",
        cache,
        "--device",
        "cpu",
        "--new_swarm",
    )
    return WorkerLaunch(
        worker_id="gpu-0",
        model_id="controlled-native",
        command=command,
        auto_start=False,
        auto_restart=True,
        restart_backoff=0.1,
        automatic=True,
        block_indices="0:1",
        placement_reason="controlled test admission",
        intent_published=True,
        remote_acknowledged=True,
        placement_manifest_digest=manifest_digest,
        placement_artifact_bytes=11,
        placement_artifact_set_digest=artifact_digest,
        placement_cache_root=cache,
        max_host_memory_bytes=1024**3,
        max_disk_bytes=1024**3,
        resource_claim=WorkerResourceClaim(
            "template", "gpu-0", 100, 200, (ArtifactClaim(cache, "weight.bin", "a" * 64, 11),)
        ),
        environment=child_environment(overlay),
        device="cpu",
    )


def direct_supervisor(manager, launch):
    from drift.node.worker_supervisor import WorkerSupervisor

    return WorkerSupervisor(
        (launch,),
        poll_period=0.02,
        stop_timeout=2,
        coordinated_launches=True,
        acquire_resources_cancellable=manager.acquire_cancellable,
        release_resources=manager.release,
        loading_binding_for_token=manager.loading_binding_for_token,
        recovery_containment_for_token=manager.recovery_containment_for_token,
    )


def retire(supervisor, manager):
    if supervisor is not None:
        supervisor.shutdown()
        if not supervisor.drain_resource_operations(timeout=10):
            raise AssertionError("native supervisor failed certified resource drain")
    if not manager.close():
        raise AssertionError("native owner lease closed before all generations drained")


def require_dead(pidfds, leaf):
    for descriptor in pidfds:
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        assert poller.poll(1000), "reservation disappeared before exact process death"
    assert "populated 0\n" in (leaf / "cgroup.events").read_text()


def quick_control(call):
    """Detect a blocked control lock without wedging the native test teardown."""
    done = threading.Event()
    outcome = []

    def run():
        try:
            outcome.append((True, call()))
        except Exception as error:
            outcome.append((False, error))
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(1), "control operation waited for the native spawn barrier"
    succeeded, value = outcome[0]
    if not succeeded:
        raise value
    return value


def wait_for_released(supervisor, private):
    # Journal publication precedes the callback returning to the supervisor.
    # A clean transition needs that final operation completion as well.
    def released():
        status = supervisor.snapshot("gpu-0")
        return not entries(private) and status["resource_operation"] is None and not status["cleanup_pending"]

    wait_for(released, supervisor=supervisor)


def test_node_builder_cli_loading_pause_and_paused_configuration_reload(managed_launch, tmp_path):
    from drift.cli import run_node
    from drift.node.linux_cgroup_process import LinuxCgroupProcess
    from drift.node.worker_loading import loading_gate
    from drift.node.worker_supervisor import WorkerReconfigurationBusyError

    fixture = managed_launch
    private = tmp_path / "native-reservations"
    private.mkdir(mode=0o700)
    overlay = child_overlay(tmp_path / "native-model-body", private)
    manager = new_manager(private, ROOT)
    supervisor = None
    pidfds = []

    def build(resources):
        result = run_node._build_worker_supervisor(
            fixture.config,
            fixture.manager,
            automatic_placements={"gpu-0": fixture.plan},
            resource_manager=resources,
        )
        launch = result.launches[0]
        assert launch.resource_claim is not None and launch.policy_admitted
        result.replace_launches((replace(launch, environment=(*launch.environment, *child_environment(overlay))),))
        result.start_service()
        return result

    try:
        supervisor = build(manager)
        assert supervisor.snapshot("gpu-0")["pid"] is None
        with loading_gate(private / "loading"):
            assert supervisor.start_worker("gpu-0") is False  # Real async admission.
            wait_for(lambda: bool(list((private / "loading").glob("*.status.json"))), supervisor=supervisor)
            status = supervisor.snapshot("gpu-0")
            assert status["state"] == "running" and status["load_state"] == "waiting"
            assert not status["model_ready"] and not (overlay / "constructed.json").exists()
            assert isinstance(supervisor._record("gpu-0").process, LinuxCgroupProcess)
            retained = entries(private)
            assert len(retained) == 1 and retained[0]["claim"]["staging_host_bytes"] > 0
            assert retained[0]["recovery"]["contract"] == "linux_cgroup_v1"
        wait_for(lambda: (overlay / "constructed.json").exists(), supervisor=supervisor)
        wait_for(lambda: supervisor.snapshot("gpu-0")["load_state"] == "loading", supervisor=supervisor)
        assert not supervisor.snapshot("gpu-0")["model_ready"]
        (overlay / "allow-ready").write_text("ready")
        wait_for(lambda: supervisor.snapshot("gpu-0")["model_ready"], supervisor=supervisor)
        body = json.loads((overlay / "constructed.json").read_text())
        descendants = json.loads((overlay / "descendant.json").read_text())
        assert body["pid"] == descendants["worker"] == supervisor.snapshot("gpu-0")["pid"]
        assert body["token"] == retained[0]["claim"]["reservation_id"]
        assert entries(private) == retained, "ready must not discount lifetime staging"
        leaf = Path(ROOT) / retained[0]["recovery"]["containment_name"]
        assert set((leaf / "cgroup.procs").read_text().split()) == {str(pid) for pid in descendants.values()}
        pidfds = [os.pidfd_open(pid) for pid in descendants.values()]
        supervisor.pause_worker("gpu-0")
        wait_for_released(supervisor, private)
        require_dead(pidfds, leaf)
        status = supervisor.snapshot("gpu-0")
        assert status["operator_paused"] and not status["desired_running"] and not status["model_ready"]
        assert not list((private / "loading").glob("*.binding.json"))

        saved_intent = tmp_path / "reload-intent.json"
        supervisor.commit_configuration_restart(lambda: write_json(saved_intent, {"start": status["desired_running"]}))
        assert supervisor.configuration_restart_pending and json.loads(saved_intent.read_text()) == {"start": False}
        with pytest.raises(WorkerReconfigurationBusyError):
            supervisor.start_worker("gpu-0")
        retire(supervisor, manager)
        supervisor = None
        manager = new_manager(private, ROOT)
        assert manager.recover()
        supervisor = build(manager)
        time.sleep(0.3)  # At least one default monitor cycle with enabled=False.
        assert supervisor.snapshot("gpu-0")["pid"] is None and not supervisor.snapshot("gpu-0")["desired_running"]
        assert entries(private) == []
        supervisor.start_worker("gpu-0")
        wait_for(lambda: supervisor.snapshot("gpu-0")["model_ready"], supervisor=supervisor)
        assert entries(private)[0]["claim"]["reservation_id"] != body["token"]
    finally:
        retire(supervisor, manager)
        for descriptor in pidfds:
            os.close(descriptor)


def test_supervised_cli_owner_hard_exit_recovers_descendants_before_fresh_start(tmp_path):
    private, cache, manifest = (tmp_path / name for name in ("private", "cache", "manifest.json"))
    cache.mkdir()
    # This case tests exact protocol claim binding; model validation/loading is
    # the controlled server seam. The preceding case uses the real node builder.
    manifest.write_text("{}")
    overlay = child_overlay(tmp_path / "child-body", private)
    (overlay / "allow-ready").write_text("ready")
    owner_ready = tmp_path / "owner-ready.json"
    helper = tmp_path / "owner.py"
    functions = (
        resource_snapshot,
        wait_for,
        entries,
        write_json,
        child_environment,
        new_manager,
        direct_launch,
        direct_supervisor,
    )
    helper.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        + "\n\n".join(inspect.getsource(function) for function in functions)
        + f"\nmanager=new_manager(Path({str(private)!r}),{ROOT!r})\n"
        + f"launch=direct_launch({str(cache)!r},{str(manifest)!r},{str(overlay)!r})\n"
        + "supervisor=direct_supervisor(manager,launch)\nsupervisor.start_service()\nsupervisor.start_worker('gpu-0')\n"
        + "wait_for(lambda:supervisor.snapshot('gpu-0')['model_ready'],supervisor=supervisor)\n"
        + f"write_json({str(owner_ready)!r},dict(owner=os.getpid(),status=supervisor.snapshot('gpu-0')))\n"
        + "time.sleep(120)\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "owner.log"
    owner_output = log_path.open("w", encoding="utf-8")
    environment = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    owner = subprocess.Popen(
        [sys.executable, str(helper)], stdout=owner_output, stderr=subprocess.STDOUT, env=environment
    )
    manager = new_manager(private, ROOT)
    supervisor = None
    pidfds = []
    try:

        def started():
            if owner.poll() is not None:
                raise AssertionError("connected owner failed: " + log_path.read_text())
            return owner_ready.exists()

        try:
            wait_for(started, seconds=90)
        except AssertionError as error:
            raise AssertionError(str(error) + "\n" + log_path.read_text()) from error
        ready = json.loads(owner_ready.read_text())
        retained = entries(private)
        descendants = json.loads((overlay / "descendant.json").read_text())
        assert ready["status"]["model_ready"] and ready["status"]["pid"] == descendants["worker"]
        leaf = Path(ROOT) / retained[0]["recovery"]["containment_name"]
        pidfds = [os.pidfd_open(pid) for pid in descendants.values()]
        assert not manager.recover() and manager.recovery_snapshot()["reason"] == "active_owner"
        assert entries(private) == retained
        owner.kill()
        owner.wait(timeout=10)
        wait_for(manager.recover)
        assert entries(private) == [] and manager.recovery_snapshot()["state"] == "ready"
        require_dead(pidfds, leaf)
        assert not list((private / "loading").glob("*.binding.json"))
        supervisor = direct_supervisor(manager, direct_launch(cache, manifest, overlay))
        supervisor.start_service()
        assert supervisor.snapshot("gpu-0")["pid"] is None
        supervisor.start_worker("gpu-0")
        wait_for(lambda: supervisor.snapshot("gpu-0")["model_ready"], supervisor=supervisor)
        assert entries(private)[0]["claim"]["reservation_id"] != retained[0]["claim"]["reservation_id"]
        supervisor.pause_worker("gpu-0")
        wait_for_released(supervisor, private)
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=10)
        owner_output.close()
        # Retain the real recovery transaction even on an assertion failure.
        if supervisor is None:
            wait_for(manager.recover)
        retire(supervisor, manager)
        for descriptor in pidfds:
            os.close(descriptor)


@pytest.mark.parametrize("boundary", ["gated_birth", "exec_acknowledgement"])
def test_native_pending_spawn_controls_cancel_without_early_resource_release(boundary, tmp_path, monkeypatch):
    from drift.node.linux_cgroup_recovery import LinuxCgroupContainment
    from drift.node.worker_loading import read_loading_status

    private, cache, manifest = (tmp_path / name for name in ("private", "cache", "manifest.json"))
    cache.mkdir()
    manifest.write_text("{}")
    overlay = child_overlay(tmp_path / "controlled-body", private)
    manager = new_manager(private, ROOT)
    entered, unblock = threading.Event(), threading.Event()
    captured = {}
    release_proofs = []
    pidfds = []
    supervisor = None
    real_spawn = LinuxCgroupContainment.spawn
    real_await_exec = LinuxCgroupContainment.await_exec
    real_release = manager.release

    def block(containment, process):
        captured.update(containment=containment, process=process)
        entered.set()
        assert unblock.wait(90), "test did not release native operation barrier"

    def gated_spawn(containment, command, **options):
        process = real_spawn(containment, command, **options)
        block(containment, process)
        return process

    def blocked_exec(containment, process, *, cancel=None):
        block(containment, process)
        return real_await_exec(containment, process, cancel=cancel)

    def verified_release(token):
        # Check the native death proof at the actual release callback boundary,
        # before the durable claim disappears, not merely after teardown.
        try:
            require_dead(pidfds, captured["leaf"])
            assert captured["process"].poll() is not None
            assert entries(private)[0]["claim"]["reservation_id"] == token
        except Exception as error:
            # Preserve the assertion for the main test, while still allowing
            # the real manager to apply its own cleanup proof during teardown.
            release_proofs.append(error)
        else:
            release_proofs.append(token)
        return real_release(token)

    monkeypatch.setattr(
        LinuxCgroupContainment,
        "spawn" if boundary == "gated_birth" else "await_exec",
        gated_spawn if boundary == "gated_birth" else blocked_exec,
    )
    monkeypatch.setattr(manager, "release", verified_release)
    try:
        supervisor = direct_supervisor(manager, direct_launch(cache, manifest, overlay))
        supervisor.start_service()
        assert supervisor.start_worker("gpu-0") is False
        assert entered.wait(60), "real native birth did not reach its controlled boundary"
        process = captured["process"]
        assert process.poll() is None
        retained = entries(private)
        assert len(retained) == 1 and retained[0]["recovery"]["contract"] == "linux_cgroup_v1"
        token = retained[0]["claim"]["reservation_id"]
        leaf = captured["leaf"] = Path(ROOT) / retained[0]["recovery"]["containment_name"]
        assert "populated 1\n" in (leaf / "cgroup.events").read_text()
        pidfds.append(os.pidfd_open(process.pid))
        status = quick_control(lambda: supervisor.snapshot("gpu-0"))
        assert status["resource_operation"] == "spawn" and status["pid"] is None and not status["model_ready"]
        if boundary == "exec_acknowledgement":
            # Exec really occurred: wait for the canonical managed CLI to enter
            # the controlled body before cancelling its unpublished generation.
            wait_for(lambda: (overlay / "constructed.json").exists(), supervisor=supervisor)
        else:
            assert not (overlay / "constructed.json").exists()

        quick_control(lambda: supervisor.pause_worker("gpu-0"))
        status = quick_control(lambda: supervisor.snapshot("gpu-0"))
        assert status["operator_paused"] and not status["desired_running"]
        assert status["resource_cancel_requested"] and status["cleanup_pending"] and not status["model_ready"]
        assert entries(private) == retained and release_proofs == []
        if boundary == "exec_acknowledgement":
            # The real child can report ready after Pause while the native exec
            # observation is delayed. That stale ticket must never be published.
            binding = manager.loading_binding_for_token(token)
            (overlay / "allow-ready").write_text("ready")
            wait_for(lambda: read_loading_status(binding, expected_pid=process.pid) == "ready")
            descendants = json.loads((overlay / "descendant.json").read_text())
            assert descendants["worker"] == process.pid
            pidfds.append(os.pidfd_open(descendants["grandchild"]))
            assert set((leaf / "cgroup.procs").read_text().split()) == {
                str(process.pid),
                str(descendants["grandchild"]),
            }
        else:
            assert set((leaf / "cgroup.procs").read_text().split()) == {str(process.pid)}
        quick_control(supervisor.shutdown)
        assert not quick_control(lambda: supervisor.drain_resource_operations(timeout=0))
        status = quick_control(lambda: supervisor.snapshot("gpu-0"))
        assert status["pid"] is None and not status["model_ready"]
        assert entries(private) == retained and release_proofs == [] and process.poll() is None

        unblock.set()
        wait_for_released(supervisor, private)
        assert supervisor.drain_resource_operations(timeout=10)
        require_dead(pidfds, leaf)
        assert release_proofs == [token]
        status = supervisor.snapshot("gpu-0")
        assert status["pid"] is None and not status["model_ready"] and status["operator_paused"]
        assert not list((private / "loading").glob("*.binding.json"))
        if boundary == "gated_birth":
            assert not (overlay / "constructed.json").exists(), "cancelled gated child executed its model body"
    finally:
        unblock.set()
        retire(supervisor, manager)
        for descriptor in pidfds:
            os.close(descriptor)
