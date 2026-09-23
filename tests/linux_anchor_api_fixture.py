"""Child runtime fixture: actual node builder/HTTP/recovery, no models or GPUs."""

import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace


def serve(profile, worker_root):
    import uvicorn

    from drift.cli import run_node
    from drift.node import linux_anchor as anchor, server as api
    from drift.node.config import NodeConfig
    from drift.node.model_manager import ModelManager
    from drift.node.worker_supervisor import WorkerSupervisor

    anchor._runtime_directory = lambda: profile.parent
    config = NodeConfig(schema_version=1, max_loaded_models=1, models=())
    run_node._load_persisted_and_runtime_config = lambda args: (None, config)
    run_node._merge_cached_initial_peers = lambda configured, cache: configured
    run_node.load_configured_catalog = lambda configured: None

    def manager(*args, **kwargs):
        value = ModelManager()
        return value, (), SimpleNamespace(start=lambda: None)

    run_node._build_model_manager = manager
    run_node._build_worker_supervisor = lambda *args, **kwargs: WorkerSupervisor(())
    run_node._build_automatic_placement_service = lambda *args, **kwargs: None
    run_node._prepare_control_key = lambda *args, **kwargs: ("fixture-control", None, False)
    # No native desktop keyring, DHT, model, GPU or release config is claimed.
    # ResourceReservationManager, storage binding, cleanup and Uvicorn are real.
    parser = run_node.build_parser()
    args = parser.parse_args(
        [
            "fixture-unused.json",
            "--data_dir",
            str(profile / "node"),
            "--worker-cgroup-root",
            worker_root,
            "--api_key",
            "fixture-client",
            "--pause_sharing_on_start",
        ]
    )
    args.port = 0
    callbacks = {}
    original_app = api.create_node_app
    original_bind = uvicorn.Config.bind_socket
    cycle = 0

    def app(*args, **kwargs):
        callbacks.update(restart=kwargs["request_restart"], shutdown=kwargs["request_shutdown"])
        return original_app(*args, **kwargs)

    def bind(self):
        result = original_bind(self)
        report = dict(cycle=cycle, port=result.getsockname()[1], pid=os.getpid())
        (profile / f"api-{cycle}.json").write_text(json.dumps(report))
        return result

    api.create_node_app = app
    uvicorn.Config.bind_socket = bind
    done = threading.Event()

    def watch():
        while not done.wait(0.02):
            for action in ("restart", "shutdown"):
                path = profile / (action + "-now")
                if path.exists() and action in callbacks:
                    path.unlink()
                    callbacks[action]()

    threading.Thread(target=watch, daemon=True).start()
    try:
        for cycle in range(1, 4):
            restart = run_node._serve_once(args, parser)
            (profile / f"closed-{cycle}").touch()
            if not restart:
                return 0
            (profile / "reload-gap").touch()
            deadline = time.monotonic() + 12
            while not (profile / "resume-reload").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("fixture reload not released")
                time.sleep(0.02)
        raise RuntimeError("unexpected third node reload")
    finally:
        done.set()
