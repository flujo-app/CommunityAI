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
        except PermissionError as exc:
            raise RuntimeError(
                "Cannot inspect process ownership; package replacement is refused. "
                "Package maintenance needs permission to inspect all processes."
            ) from exc
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
    identities = {}

    def remaining():
        current = process_snapshot()
        selected = {
            pid
            for pid, (_, started, exe) in current.items()
            if pid != os.getpid() and (exe.is_relative_to(root) or identities.get(pid) == started)
        }
        while True:
            descendants = {pid for pid, (parent, _, _) in current.items() if parent in selected and pid != os.getpid()}
            if descendants <= selected:
                break
            selected |= descendants
        identities.update({pid: current[pid][1] for pid in selected})
        return selected

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

    for signum, duration in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5)):
        deadline = time.monotonic() + duration
        signaled = set()
        quiet_since = None
        while time.monotonic() < deadline:
            live = remaining()
            now = time.monotonic()
            if live:
                quiet_since = None
                new = {pid for pid in live if (pid, identities[pid]) not in signaled}
                send(new, signum)
                signaled.update((pid, identities[pid]) for pid in new)
            else:
                quiet_since = now if quiet_since is None else quiet_since
                if now - quiet_since >= 0.3:
                    return
            time.sleep(0.1)
    raise RuntimeError("CommunityAI processes did not stay stopped; package replacement is refused")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("install", "upgrade", "remove", "deconfigure"):
        stop_installation()
