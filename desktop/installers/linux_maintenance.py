#!/usr/bin/python3
"""Stop only processes belonging to the installed CommunityAI tree before dpkg changes it."""

import os
import signal
import sys
import time
from pathlib import Path


def process_snapshot():
    result = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            executable = Path(os.readlink(directory / "exe").removesuffix(" (deleted)"))
            result[int(directory.name)] = (int(fields[1]), fields[19], executable)
        except (OSError, ValueError, IndexError):
            continue
    return result


def stop_installation(root=Path("/opt/communityai"), timeout=30):
    root = root.resolve()
    if not root.exists():
        return
    marker = root / ".communityai-installation"
    if not marker.is_file() or not marker.read_text().startswith("CommunityAI installer-managed"):
        raise RuntimeError("The installation directory is not marked as CommunityAI-owned")
    before = process_snapshot()
    selected = {pid for pid, (_, _, exe) in before.items() if exe.is_relative_to(root) and pid != os.getpid()}
    while True:
        descendants = {pid for pid, (parent, _, _) in before.items() if parent in selected and pid != os.getpid()}
        if descendants <= selected:
            break
        selected |= descendants
    identities = {pid: before[pid][1] for pid in selected}

    def remaining():
        current = process_snapshot()
        return {pid for pid in identities if pid in current and current[pid][1] == identities[pid]}

    def send(pids, signum):
        for pid in pids:
            try:
                descriptor = os.pidfd_open(pid)
                try:
                    current = process_snapshot()
                    if pid in current and current[pid][1] == identities[pid]:
                        signal.pidfd_send_signal(descriptor, signum)
                finally:
                    os.close(descriptor)
            except ProcessLookupError:
                pass

    send(remaining(), signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while remaining() and time.monotonic() < deadline:
        time.sleep(0.1)
    send(remaining(), signal.SIGKILL)
    deadline = time.monotonic() + 5
    while remaining() and time.monotonic() < deadline:
        time.sleep(0.1)
    if remaining():
        raise RuntimeError("CommunityAI processes remain; package replacement is refused")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("install", "upgrade", "remove", "deconfigure"):
        stop_installation()
