"""Exercise desktop-to-node native credentials and lifecycle with a real local process."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import uuid
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "desktop" / "src"))

from communityai_desktop.credentials import NativeCredentialStore  # noqa: E402
from communityai_desktop.lifecycle import NodeLifecycleSupervisor  # noqa: E402

PUBLIC_BOOTSTRAP = (
    "/dns4/bootstrap.communityai.flujo.com.co/tcp/31337/" "p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo"
)


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--node-command",
        type=Path,
        help="Packaged CommunityAI-Node executable to supervise instead of the source drift command",
    )
    args = parser.parse_args()
    node_command = None
    if args.node_command is not None:
        executable = args.node_command.expanduser().resolve()
        if not executable.is_file():
            parser.error(f"node executable does not exist: {executable}")
        node_command = (str(executable),)

    service = f"org.communityai.desktop.smoke.{uuid.uuid4().hex}"
    account = "local-node-control-v1"
    store = NativeCredentialStore(service, account)
    with tempfile.TemporaryDirectory(prefix="communityai-managed-node-") as directory:
        root = Path(directory)
        config_path = root / "node-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "max_loaded_models": 1,
                    "discovery_update_period": 30,
                    "discovery_startup_timeout": 2,
                    "models": [
                        {
                            "manifest": str(
                                (REPOSITORY_ROOT / "tests" / "data" / "model_manifest_v1_vector.json").resolve()
                            ),
                            "initial_peers": [PUBLIC_BOOTSTRAP],
                        }
                    ],
                    "workers": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        port = _unused_loopback_port()
        supervisor = NodeLifecycleSupervisor(
            f"http://127.0.0.1:{port}",
            store,
            config_path=config_path,
            data_dir=root / "data",
            node_command=node_command,
            startup_timeout=45,
        )
        try:
            client = supervisor.ensure_client()
            status = client.status()
            result = {
                "api_version": status["api_version"],
                "credential_file_created": (root / "data" / "control-api.key").exists(),
                "model_count": len(status["models"]),
                "owned_pid": supervisor.owned_pid,
                "public_bootstrap": PUBLIC_BOOTSTRAP,
            }
            if result["credential_file_created"]:
                raise RuntimeError("desktop-owned node wrote its privileged native credential to a file")
            if result["owned_pid"] is None:
                raise RuntimeError("desktop did not retain ownership of the local node")
            print(json.dumps(result, sort_keys=True))
        finally:
            supervisor.close()
            store.delete()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
