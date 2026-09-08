"""Embedded, isolated root helper: verify a protected copy before handing it to APT."""

import contextlib
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_PACKAGE_BYTES = 32 * 1024**3
CHUNK_BYTES = 1024 * 1024


@contextlib.contextmanager
def copy_deadline():
    def interrupted(signum, frame):
        raise ValueError("Protected package copy cancelled or timed out")

    alarm = signal.signal(signal.SIGALRM, interrupted)
    term = signal.signal(signal.SIGTERM, interrupted)
    signal.setitimer(signal.ITIMER_REAL, 2 * 60 * 60)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, alarm)
        signal.signal(signal.SIGTERM, term)


def protected_copy(source, destination, expected_size, expected_sha256):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    received = 0
    with os.fdopen(descriptor, "rb") as incoming, destination.open("xb") as output:
        metadata = os.fstat(incoming.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise ValueError("Source package is not the expected regular file")
        while True:
            block = incoming.read(min(CHUNK_BYTES, expected_size - received + 1))
            if not block:
                break
            received += len(block)
            if received > expected_size:
                raise ValueError("Package grew during protected copy")
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if received != expected_size or digest.hexdigest() != expected_sha256:
        raise ValueError("Protected package size or SHA-256 verification failed")


def run(source, filename, expected_size, expected_sha256, *, directory=Path("/var/tmp"), popen=subprocess.Popen):
    if os.geteuid() != 0:
        raise ValueError("The protected installation helper requires root")
    if not re.fullmatch(r"communityai_[0-9][A-Za-z0-9.+~\-]*_amd64\.deb", filename):
        raise ValueError("Invalid package filename")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or not 0 < expected_size <= MAX_PACKAGE_BYTES:
        raise ValueError("Invalid pinned package identity")
    if shutil.disk_usage(directory).free < expected_size + 64 * 1024 * 1024:
        raise ValueError("Insufficient disk space for the protected package copy")
    staging = Path(tempfile.mkdtemp(prefix="communityai-install-", dir=directory))
    package = staging / filename
    keep_staging = False
    try:
        # The copy's root-owned directory prevents an ordinary-user writer from
        # changing bytes after verification, including through an old open fd.
        with copy_deadline():
            protected_copy(source, package, expected_size, expected_sha256)
        package.chmod(0o644)
        staging.chmod(0o755)
        print("Protected package size and SHA-256 verified. Starting APT.")
        keep_staging = True
        child = popen(["/usr/bin/apt", "install", str(package)])
        try:
            code = child.wait()
        except KeyboardInterrupt:
            print("Waiting for APT to finish safely; do not kill the package manager.", file=sys.stderr)
            code = child.wait()
        keep_staging = False
        return code
    finally:
        if keep_staging:
            print("APT exit was not confirmed; retained protected package: " + str(package), file=sys.stderr)
        else:
            shutil.rmtree(staging)


def main():
    if len(sys.argv) != 5:
        raise SystemExit("Expected source package, filename, byte count and SHA-256")
    try:
        return run(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4])
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        print("Protected installation stopped: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
