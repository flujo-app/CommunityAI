"""Bounded Modal raw-TCP reachability check before launching model containers."""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import modal


def confirm_app_stopped(app_id, timeout=60):
    """Modal's app list is eventually consistent after a successful stop."""
    subprocess.run(
        [sys.executable, "-m", "modal", "app", "stop", "--yes", app_id],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    observations = []
    until = time.time() + timeout
    while time.time() < until:
        listed = subprocess.run(
            [sys.executable, "-m", "modal", "app", "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        record = next((v for v in json.loads(listed.stdout) if v["app_id"] == app_id), None)
        observations.append(record)
        if record and record["state"].casefold() == "stopped" and int(record["tasks"]) == 0:
            return {"verified": True, "observations": observations}
        time.sleep(2)
    return {"verified": False, "observations": observations}


def main():
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    run_id = time.strftime("q38mt-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)
    path = root / ".gate13-runs/qwen-modal-transport" / run_id
    path.mkdir(parents=True, exist_ok=False)
    result = {"result": "failed", "run_id": run_id, "scope": "raw-tcp-nonce-probe", "model_inference": False}
    sandbox = None
    app = modal.App(run_id)
    program = (
        "import socket\n"
        "s=socket.socket(); s.bind(('0.0.0.0',31330)); s.listen()\n"
        "while True:\n"
        " c,a=s.accept(); c.settimeout(5)\n"
        " try: c.sendall(b'communityai:'+c.recv(64))\n"
        " except OSError: pass\n"
        " finally: c.close()\n"
    )
    try:
        with modal.enable_output(), app.run():
            try:
                sandbox = modal.Sandbox.create(
                    "python",
                    "-u",
                    "-c",
                    program,
                    app=app,
                    image=modal.Image.debian_slim(python_version="3.12"),
                    cpu=(0.125, 0.25),
                    memory=(128, 256),
                    timeout=180,
                    unencrypted_ports=[31330],
                    tags={"communityai-run": run_id},
                )
                result["sandbox_id"] = sandbox.object_id
                (path / "resource.json").write_text(json.dumps(result), encoding="utf-8")
                address = sandbox.tunnels()[31330].tcp_socket
                nonce = secrets.token_hex(12).encode()
                until = time.time() + 60
                while time.time() < until:
                    try:
                        with socket.create_connection(address, timeout=10) as connection:
                            connection.sendall(nonce)
                            reply = connection.recv(128)
                        if reply != b"communityai:" + nonce:
                            raise RuntimeError("Modal tunnel nonce response mismatch")
                        break
                    except OSError:
                        time.sleep(2)
                else:
                    raise TimeoutError("Modal TCP tunnel did not become reachable")
                result.update(result="passed", tcp_nonce_verified=True)
            finally:
                if sandbox is not None:
                    sandbox.terminate(wait=True)
                    result["cleanup_verified"] = sandbox.poll() is not None
    except BaseException as exc:
        result.update(result="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        if app.app_id:
            result["app_id"] = app.app_id
            result["app_cleanup"] = confirm_app_stopped(app.app_id)
            result["app_stop_confirmed"] = result["app_cleanup"]["verified"]
            result["cleanup_verified"] = result["app_stop_confirmed"] and (
                sandbox is None or sandbox.poll() is not None
            )
    if not result.get("cleanup_verified"):
        result["result"] = "failed"
    (path / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": result["result"], "evidence": str(path / "result.json")}), flush=True)
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
