"""Short process-tree containment experiment; no vLLM, model or GPU required."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from drift.node.edge_supervisor import _force_containment_exit, _new_containment


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="communityai-vllm-owner-") as temporary:
        ready = Path(temporary) / "ready"
        code = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
        )
        containment = _new_containment()
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(ready)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **containment.popen_kwargs(),
            )
            containment.attach(process)
            containment.resume(process)
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists() and containment.has_members(), "parent and child must be contained"
            assert _force_containment_exit(containment, process, 0.02), "complete tree exit was not proved"
            assert not containment.has_members(), "orphaned process remained"
            print("PASS: fake parent and child exit within bounded containment cleanup")
        finally:
            if process is not None and process.poll() is None:
                _force_containment_exit(containment, process, 0.02)
            containment.close()


if __name__ == "__main__":
    main()
