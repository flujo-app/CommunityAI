#!/usr/bin/env python3
"""Download the pinned CommunityAI Debian package, verify it, then install through APT."""

import argparse
import contextlib
import hashlib
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ARTIFACT = None  # Replaced with a validated release entry by build_linux_online.py.
ROOT_HELPER = None  # Replaced with the isolated protected-copy helper by the builder.
CHUNK_BYTES = 1024 * 1024
READ_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 2 * 60 * 60
DISK_MARGIN_BYTES = 64 * 1024 * 1024


class InstallError(RuntimeError):
    pass


class Cancelled(InstallError):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise InstallError("The download origin redirected the request; no redirected content was downloaded.")


@contextlib.contextmanager
def download_deadline():
    """Bound DNS/connect/read time too; the supported installer platform is Linux."""

    def expired(signum, frame):
        raise InstallError("The two-hour download deadline expired; rerun the installer to retry.")

    def cancelled(signum, frame):
        raise Cancelled("Download cancelled.")

    previous_alarm = signal.signal(signal.SIGALRM, expired)
    previous_term = signal.signal(signal.SIGTERM, cancelled)
    signal.setitimer(signal.ITIMER_REAL, DOWNLOAD_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        signal.signal(signal.SIGTERM, previous_term)


def download(artifact, destination, *, opener=None, clock=time.monotonic, progress=print):
    """Stream one exact object; no resume, redirects, credentials, or remote manifest."""
    opener = opener or urllib.request.build_opener(NoRedirects())
    request = urllib.request.Request(
        artifact["url"], headers={"Accept-Encoding": "identity", "User-Agent": "CommunityAI-Online-Installer/1"}
    )
    started = clock()
    digest = hashlib.sha256()
    received = 0
    next_progress = 0
    with opener.open(request, timeout=READ_TIMEOUT_SECONDS) as response:
        if response.status != 200 or response.geturl() != artifact["url"]:
            raise InstallError("The server did not return the exact pinned download URL.")
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding != "identity":
            raise InstallError("The server returned an encoded download instead of the pinned package bytes.")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) != artifact["size_bytes"]):
            raise InstallError("The server's package size does not match this installer.")
        with destination.open("xb") as output:
            while True:
                if clock() - started >= DOWNLOAD_TIMEOUT_SECONDS:
                    raise InstallError("The download deadline expired.")
                chunk = response.read(min(CHUNK_BYTES, artifact["size_bytes"] - received + 1))
                if not chunk:
                    break
                received += len(chunk)
                if received > artifact["size_bytes"]:
                    raise InstallError("The download exceeds the pinned package size.")
                digest.update(chunk)
                output.write(chunk)
                percent = received * 100 // artifact["size_bytes"]
                if percent >= next_progress:
                    progress("Downloaded {}% ({:,} / {:,} bytes)".format(percent, received, artifact["size_bytes"]))
                    next_progress = (percent // 5 + 1) * 5
            output.flush()
            os.fsync(output.fileno())
    if received != artifact["size_bytes"]:
        raise InstallError("The download is incomplete; rerun the installer to retry.")
    if digest.hexdigest() != artifact["sha256"]:
        raise InstallError("SHA-256 verification failed; the package will not be installed.")
    return destination


def install_command(package, artifact):
    # Fixed system paths avoid resolving a privileged command through a user PATH.
    return [
        "/usr/bin/sudo",
        "/usr/bin/python3",
        "-I",
        "-c",
        ROOT_HELPER,
        str(package.resolve()),
        artifact["filename"],
        str(artifact["size_bytes"]),
        artifact["sha256"],
    ]


def install_verified(package, artifact, *, popen=subprocess.Popen):
    """APT keeps its normal prompts and owns package-manager recovery on interruption."""
    child = popen(install_command(package, artifact))
    try:
        return child.wait()
    except KeyboardInterrupt:
        # The terminal also sends SIGINT to sudo/APT. Do not kill dpkg or remove
        # its input while it may still be active. A second interruption escapes
        # and the caller conservatively keeps this one owned staging directory.
        print("Installation interrupted; waiting for the package manager to finish safely.", file=sys.stderr)
        return child.wait()


def run(artifact, *, directory=Path("/var/tmp"), download_only=False):
    if sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise InstallError("This installer requires amd64 Debian 12+ or Ubuntu 22.04+.")
    if os.geteuid() == 0:
        raise InstallError("Run this script as your ordinary user. It requests sudo only after verifying the package.")
    if not directory.is_dir():
        raise InstallError("The download directory must already exist.")
    if not download_only and not all(
        Path(path).is_file() for path in ("/usr/bin/sudo", "/usr/bin/apt", "/usr/bin/python3")
    ):
        raise InstallError(
            "APT, sudo and system Python 3 are required. Use --download-only to retain the verified package."
        )
    download_space = artifact["size_bytes"] * (1 if download_only else 2) + DISK_MARGIN_BYTES
    if shutil.disk_usage(directory).free < download_space:
        raise InstallError(
            "The download directory needs at least {:,} free bytes for download and protected staging.".format(
                download_space
            )
        )
    print("CommunityAI {} for Debian/Ubuntu amd64".format(artifact["version"]))
    print(
        "Download: {:,} bytes. The complete runtime is included; model weights download separately.".format(
            artifact["size_bytes"]
        )
    )
    print("SHA-256: " + artifact["sha256"])
    print("Settings and model cache follow the existing package's normal retention policy.")
    if not download_only:
        print(
            "Installation verifies a protected copy and needs temporary space for a second package, plus the installed runtime."
        )
    staging = Path(tempfile.mkdtemp(prefix="communityai-online-", dir=directory))
    keep_staging = False
    package = staging / artifact["filename"]
    try:
        with download_deadline():
            download(artifact, package)
        print("Package size and SHA-256 verified.")
        if download_only:
            keep_staging = True
            print("Verified package saved to: " + str(package))
            return 0
        keep_staging = True
        code = install_verified(package, artifact)
        keep_staging = False  # wait() confirmed that the package manager exited.
        if code != 0:
            raise InstallError(
                "APT exited with status {}. Follow its recovery instructions before retrying.".format(code)
            )
        print("CommunityAI is installed. Launch it from the application menu as your ordinary user.")
        return 0
    finally:
        if keep_staging:
            print("Retained installer package: " + str(package))
        else:
            # Only the directory returned by this invocation's mkdtemp is removed.
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true", help="retain the verified package without invoking APT")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path("/var/tmp"),
        help="existing staging directory with enough download space (default: /var/tmp)",
    )
    args = parser.parse_args(argv)
    if ARTIFACT is None:
        parser.error("This is an unconfigured template; generate an installer with build_linux_online.py.")
    try:
        return run(ARTIFACT, directory=args.directory, download_only=args.download_only)
    except KeyboardInterrupt:
        print("Cancelled. No unverified package was installed.", file=sys.stderr)
        return 130
    except Cancelled as exc:
        print(str(exc), file=sys.stderr)
        return 143
    except (InstallError, OSError, urllib.error.URLError) as exc:
        print("Installation stopped: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
