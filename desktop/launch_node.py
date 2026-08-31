"""Frozen entry point for the standalone CommunityAI node sidecar.

The sidecar normally runs ``drift node``. A frozen node also reuses this
executable for supervised contribution workers and first-install catalog
bootstrap, so the explicit modes below replace their ``python -m drift.cli``
forms inside a packaged installation.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from importlib.metadata import version
from importlib.resources import files


def _runtime_contract() -> dict[str, object]:
    """Import every critical packaged runtime and locate Hivemind's daemon."""
    import fastapi
    import hivemind
    import keyring
    import torch
    import transformers
    import uvicorn

    import drift
    from drift.node.catalog_bootstrap import CATALOG_BOOTSTRAP_SCHEMA_VERSION

    daemon_name = "p2pd.exe" if os.name == "nt" else "p2pd"
    daemon = files("hivemind.hivemind_cli").joinpath(daemon_name)
    if not daemon.is_file():
        raise RuntimeError(f"packaged Hivemind daemon is missing: {daemon_name}")
    return {
        "schema_version": 1,
        "application": "CommunityAI-Node",
        "drift": drift.__version__,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "hivemind": hivemind.__version__,
        "fastapi": fastapi.__version__,
        "uvicorn": uvicorn.__version__,
        "keyring": version("keyring"),
        "p2pd": daemon_name,
        "catalog_bootstrap_schema": CATALOG_BOOTSTRAP_SCHEMA_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _worker_runtime_contract() -> dict[str, object]:
    """Exercise the frozen worker entry point without loading weights or joining a network."""
    from drift.cli.run_server import build_parser
    from drift.server.admission import AdmissionPolicy
    from drift.server.server import Server
    from drift.utils.process_lifetime import tie_child_processes_to_this_process

    parser = build_parser()
    parsed = vars(parser.parse_args(["qualification/model", "--new_swarm", "--throughput", "dry_run"]))
    policy = AdmissionPolicy()
    if parsed["model"] != "qualification/model" or parsed["new_swarm"] is not True:
        raise RuntimeError("packaged worker parser did not retain the bounded self-test contract")
    if (
        parsed["throughput"] != "dry_run"
        or parsed["allow_training_rpcs"] is not False
        or policy.allow_training_rpcs is not False
    ):
        raise RuntimeError("packaged worker defaults are not bounded")
    if not tie_child_processes_to_this_process():
        raise RuntimeError("packaged worker process-lifetime guard could not be armed")
    return {
        "schema_version": 1,
        "application": "CommunityAI-Worker",
        "entrypoint": "server",
        "server_class": Server.__name__,
        "model_loading_performed": False,
        "network_join_performed": False,
        "throughput_mode": parsed["throughput"],
        "training_rpcs_enabled": parsed["allow_training_rpcs"],
        "process_lifetime_guard_armed": True,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def main() -> int:
    # PyInstaller's multiprocessing children must be intercepted before importing
    # Torch, Hivemind, or any application modules.
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if argv == ["--self-test"]:
        print(json.dumps(_runtime_contract(), sort_keys=True))
        return 0
    if argv == ["server", "--self-test"]:
        print(json.dumps(_worker_runtime_contract(), sort_keys=True))
        return 0
    if argv[:1] == ["server"]:
        sys.argv = ["CommunityAI-Node server", *argv[1:]]
        from drift.cli.run_server import main as run
    elif argv[:1] == ["edge-acquire"]:
        sys.argv = ["CommunityAI-Node edge-acquire", *argv[1:]]
        from drift.cli.run_edge_acquisition import main as run
    elif argv[:1] == ["bootstrap"]:
        sys.argv = ["CommunityAI-Node bootstrap", *argv[1:]]
        from drift.cli.run_bootstrap import main as run
    else:
        from drift.cli.run_node import main as run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
