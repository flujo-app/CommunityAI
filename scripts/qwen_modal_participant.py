"""Start/kill an owned node and real Xvfb desktop inside one Modal sandbox."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path("/srv/q38")
SOURCE = Path("/opt/q38/source")


def main(action):
    record = ROOT / "participant-processes.json"
    if action == "start":
        for name in ("formation-error.json", "desktop-error.json", "desktop-stop"):
            (ROOT / name).unlink(missing_ok=True)
        processes = []
        for script in ("qwen_formation_node.py", "qwen_formation_desktop.py"):
            command = [sys.executable, str(SOURCE / "scripts" / script), "--root", str(ROOT)]
            if "desktop" in script:
                command = ["xvfb-run", "-a", "-s", "-screen 0 1440x1000x24", *command]
            with (ROOT / (script + ".log")).open("ab") as log:
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=dict(
                        os.environ, PYTHONPATH=str(SOURCE / "desktop/src"), OMP_NUM_THREADS="4", MKL_NUM_THREADS="4"
                    ),
                )
            processes.append({"pid": process.pid, "created": psutil.Process(process.pid).create_time()})
        record.write_text(json.dumps(processes))
        return {"started": time.time(), "processes": processes}
    if action != "stop":
        raise ValueError("Unknown participant action")
    targets = {}
    for original in json.loads(record.read_text()):
        try:
            parent = psutil.Process(original["pid"])
            if parent.create_time() != original["created"]:
                raise RuntimeError("Owned participant PID was reused")
            for process in [*parent.children(recursive=True), parent]:
                targets[process.pid] = process
        except psutil.NoSuchProcess:
            pass
    for process in targets.values():
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(targets.values()), timeout=20)
    live = [p.pid for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
    if live:
        raise RuntimeError("Participant processes survived loss: " + str(live))
    return {"verified": True, "killed_pids": list(targets), "scope": "complete node and desktop process trees"}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1])))
