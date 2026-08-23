"""Frozen entry point for the standalone CommunityAI node sidecar.

The sidecar normally runs ``drift node``. A frozen node also reuses this
executable for supervised contribution workers, so the explicit ``server`` mode
below replaces ``python -m drift.cli server`` inside a packaged installation.
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
    import drift
    import fastapi
    import hivemind
    import keyring
    import torch
    import transformers
    import uvicorn

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
    if argv[:1] == ["server"]:
        sys.argv = ["CommunityAI-Node server", *argv[1:]]
        from drift.cli.run_server import main as run
    else:
        from drift.cli.run_node import main as run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
