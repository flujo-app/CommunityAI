"""Authenticated application updates, resumable downloads and installer handoff."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from communityai_desktop.release import RELEASE_VERSION
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ORIGIN = "https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev"
FEEDS = (
    ORIGIN + "/updates/alpha.json",
    "https://raw.githubusercontent.com/flujo-app/CommunityAI/codex/gate14-20260902-b/public-alpha/updates/alpha.json",
)
PUBLIC_KEY = "9gREIiEXMW20DgpIFW1Hjzsb3hFSZ2Ya3Kx6oSTZuz0="
SIGNATURE_DOMAIN = b"communityai-app-update-v1\x00"
MAX_FEED_BYTES = 65536
MAX_PACKAGE_BYTES = 32 * 1024**3


class UpdateError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise UpdateError(message)


def version_key(value):
    require(isinstance(value, str), "Invalid application version")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-alpha\.(\d{8})\.(\d+))?", value)
    require(match is not None, "Invalid application version")
    major, minor, patch, date, revision = match.groups()
    return int(major), int(minor), int(patch), int(date is None), int(date or 0), int(revision or 0)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def unique_fields(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "Duplicate update field")
        value[key] = item
    return value


def verify_feed(raw, *, public_key=PUBLIC_KEY, now=None, minimum_sequence=0):
    require(len(raw) <= MAX_FEED_BYTES, "Update feed is too large")
    try:
        envelope = json.loads(raw, object_pairs_hook=unique_fields)
        require(isinstance(envelope, dict) and set(envelope) == {"signed", "signature"}, "Invalid signed update")
        signed = envelope["signed"]
        signature = base64.b64decode(envelope["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
            signature, SIGNATURE_DOMAIN + canonical(signed)
        )
    except (InvalidSignature, TypeError, KeyError, ValueError) as exc:
        raise UpdateError("Update signature could not be verified") from exc
    require(
        isinstance(signed, dict)
        and set(signed)
        == {"schema_version", "channel", "version", "sequence", "published_at", "expires_at", "artifacts"},
        "Invalid update fields",
    )
    require(type(signed["schema_version"]) is int and signed["schema_version"] == 1, "Unsupported update format")
    require(signed["channel"] == "alpha", "Wrong update channel")
    version_key(signed["version"])
    sequence = signed["sequence"]
    require(type(sequence) is int and sequence >= minimum_sequence and sequence > 0, "Older update feed rejected")
    now = time.time() if now is None else now
    issued, expires = signed["published_at"], signed["expires_at"]
    require(
        type(issued) is int
        and type(expires) is int
        and issued <= now + 300
        and now < expires
        and 0 < expires - issued <= 180 * 86400,
        "Update feed is expired or not yet valid",
    )
    artifacts = signed["artifacts"]
    require(isinstance(artifacts, dict) and 1 <= len(artifacts) <= 2, "Invalid update packages")
    for target, item in artifacts.items():
        require(target in ("windows-x64", "linux-amd64") and isinstance(item, dict), "Unsupported update platform")
        require(set(item) == {"filename", "url", "size_bytes", "sha256"}, "Invalid update package fields")
        expected = (
            f"communityai-{signed['version']}-windows-setup.exe"
            if target == "windows-x64"
            else f"communityai_{signed['version'].replace('-alpha.', '~alpha.')}_amd64.deb"
        )
        require(item["filename"] == expected, "Update filename does not match version")
        require(type(item["size_bytes"]) is int and 0 < item["size_bytes"] <= MAX_PACKAGE_BYTES, "Invalid update size")
        require(
            isinstance(item["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]), "Invalid update hash"
        )
        parsed = urlsplit(item["url"])
        require(
            parsed.scheme == "https"
            and parsed.netloc == urlsplit(ORIGIN).netloc
            and not parsed.query
            and not parsed.fragment
            and parsed.path.startswith("/alpha/")
            and parsed.path.rsplit("/", 1)[-1] == expected
            and "/../" not in parsed.path
            and "%" not in parsed.path,
            "Update package has an untrusted address",
        )
    return signed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateError("Update redirects are not allowed")


def open_url(url, *, offset=0):
    headers = {"User-Agent": "CommunityAI-Online-Installer/1", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    return urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url, headers=headers), timeout=20)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical(value))
    os.replace(temporary, path)


def download(item, directory, progress, cancelled, *, opener=open_url):
    directory.mkdir(parents=True, exist_ok=True)
    require(not directory.is_symlink(), "Invalid update cache")
    target = directory / item["filename"]
    partial = directory / (item["filename"] + ".part")
    require(not target.is_symlink() and not partial.is_symlink(), "Invalid cached update")
    size = item["size_bytes"]
    if target.is_file() and target.stat().st_size == size and file_hash(target) == item["sha256"]:
        return target
    if target.is_file():
        target.unlink()
    if partial.exists() and partial.stat().st_size > size:
        partial.unlink()
    for attempt in range(4):
        if cancelled.is_set():
            raise UpdateError("Update download paused")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            break
        require(
            shutil.disk_usage(directory).free >= size - offset + 256 * 1024**2, "Not enough storage for the update"
        )
        try:
            with opener(item["url"], offset=offset) as response:
                require(response.headers.get("Content-Encoding", "identity") == "identity", "Encoded update rejected")
                if response.status == 200:
                    offset = 0  # A server may ignore Range; restart rather than append.
                    mode = "wb"
                else:
                    require(response.status == 206 and offset > 0, "Unexpected update response")
                    require(
                        response.headers.get("Content-Range") == f"bytes {offset}-{size - 1}/{size}",
                        "Invalid update range",
                    )
                    mode = "ab"
                require(response.headers.get("Content-Length") == str(size - offset), "Update size mismatch")
                with partial.open(mode) as stream:
                    while offset < size:
                        if cancelled.is_set():
                            raise UpdateError("Update download paused")
                        block = response.read(min(1024**2, size - offset))
                        if not block:
                            raise OSError("Update download interrupted")
                        stream.write(block)
                        offset += len(block)
                        progress(offset, size)
                    require(not response.read(1), "Update exceeded its expected size")
            break
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            if attempt == 3:
                raise UpdateError("Download interrupted. It will resume when you retry.")
            if cancelled.wait(min(2**attempt, 4)):
                raise UpdateError("Update download paused")
    require(partial.is_file() and partial.stat().st_size == size, "Update download is incomplete")
    if file_hash(partial) != item["sha256"]:
        partial.unlink()
        raise UpdateError("Update verification failed. Please retry.")
    os.replace(partial, target)
    return target


def installed_root():
    if not getattr(sys, "frozen", False):
        return None
    root = Path(sys.executable).resolve().parent
    marker = root / ".communityai-installation"
    if marker.is_file() and marker.read_text(encoding="utf-8").startswith("CommunityAI installer-managed"):
        return root
    return None


class UpdateManager:
    def __init__(self, directory, *, root=None, current_version=RELEASE_VERSION, system=None):
        self.directory = Path(directory)
        self.root = root
        self.current_version = current_version
        self.system = system or platform.system()
        self.target = "windows-x64" if self.system == "Windows" else "linux-amd64"
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.state = {"status": "idle", "message": "Check for updates", "version": current_version}
        self.thread = None
        self.candidate = None
        self.process = None

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def check(self):
        if self.thread and self.thread.is_alive() or self.snapshot()["status"] == "installing":
            return
        self.cancelled.clear()
        self._set(status="checking", message="Checking for updates…")
        self.thread = threading.Thread(target=self._check, daemon=True, name="CommunityAI updates")
        self.thread.start()

    def _check(self):
        try:
            state_path = self.directory / "accepted.json"
            minimum = 0
            if state_path.exists():
                accepted = json.loads(state_path.read_bytes())
                minimum = int(accepted.get("sequence", 0))
            errors = []
            signed = None
            for url in FEEDS:
                try:
                    with open_url(url) as response:
                        raw = response.read(MAX_FEED_BYTES + 1)
                    signed = verify_feed(raw, minimum_sequence=minimum)
                    break
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    errors.append(type(exc).__name__)
            require(signed is not None, "Could not check for updates. Try again later.")
            atomic_json(state_path, {"sequence": signed["sequence"]})
            if (
                version_key(signed["version"]) <= version_key(self.current_version)
                or self.target not in signed["artifacts"]
            ):
                self._clear_installed_downloads()
                self._set(status="current", message="You’re up to date")
                return
            item = signed["artifacts"][self.target]
            self._set(status="downloading", message="Downloading update…", version=signed["version"])
            path = download(
                item,
                self.directory / item["sha256"],
                lambda received, total: self._set(message=f"Downloading update… {received * 100 // total}%"),
                self.cancelled,
            )
            self.candidate = (signed, path)
            self._set(status="ready", message="Restart to update", version=signed["version"])
        except Exception as exc:
            self._set(
                status="error", message=str(exc) if isinstance(exc, UpdateError) else "Could not update. Try again."
            )

    def install(self):
        require(self.root is not None and self.candidate is not None, "Update is not ready")
        require(not self.thread or not self.thread.is_alive(), "Update is still being checked")
        self._set(status="installing", message="Installing update…")
        self.thread = threading.Thread(target=self._install, daemon=True, name="CommunityAI install")
        self.thread.start()

    def _clear_installed_downloads(self):
        for directory in self.directory.iterdir():
            if not re.fullmatch(r"[0-9a-f]{64}", directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            for file in directory.iterdir():
                match = re.fullmatch(
                    r"communityai-(.+)-windows-setup\.exe(?:\.part)?|communityai_(.+)_amd64\.deb(?:\.part)?", file.name
                )
                if not match or file.is_symlink() or not file.is_file():
                    continue
                version = (match[1] or match[2]).replace("~alpha.", "-alpha.")
                if version_key(version) <= version_key(self.current_version):
                    try:
                        file.unlink()
                    except OSError:
                        pass  # Windows may still have its installer open during restart.
            try:
                directory.rmdir()
            except OSError:
                pass

    def _install(self):
        try:
            signed, path = self.candidate
            require(time.time() < signed["expires_at"], "Update information expired. Check for updates again.")
            item = signed["artifacts"][self.target]
            require(
                not path.is_symlink()
                and path.stat().st_size == item["size_bytes"]
                and file_hash(path) == item["sha256"],
                "Cached update changed. Please download it again.",
            )
            if self.system == "Windows":
                arguments = [
                    str(path),
                    "/SILENT",
                    "/SP-",
                    "/NORESTART",
                    "/UPDATE=1",
                    "/DIR=" + str(self.root),
                    "/LOG=" + str(self.directory / "install.log"),
                ]
                self.process = subprocess.Popen(arguments, creationflags=subprocess.CREATE_NO_WINDOW)
                code = self.process.wait()
                require(code == 0, "Update installation did not finish. Try again.")
            else:
                require(
                    self.root == Path("/opt/communityai") and Path("/usr/bin/pkexec").is_file(),
                    "Install policykit-1 to allow app updates.",
                )
                result = self.directory / "install-result.txt"
                result.unlink(missing_ok=True)
                # Reparent before APT starts: package maintenance must never kill its own installer as a GUI child.
                command = (
                    "(sleep 1; /usr/bin/pkexec /usr/bin/python3 -I /opt/communityai/update_install.py "
                    '"$1" "$2" "$3" "$4" --noninteractive; code=$?; printf "%s" "$code" >"$5"; '
                    'if [ "$code" -eq 0 ]; then exec /usr/bin/communityai; fi) </dev/null >>"$6" 2>&1 &'
                )
                launcher = subprocess.Popen(
                    [
                        "/bin/sh",
                        "-c",
                        command,
                        "communityai-update",
                        str(path),
                        item["filename"],
                        str(item["size_bytes"]),
                        item["sha256"],
                        str(result),
                        str(self.directory / "install.log"),
                    ],
                    start_new_session=True,
                )
                require(launcher.wait(timeout=10) == 0, "Could not start the update installer")
                while not result.exists() and not self.cancelled.wait(1):
                    pass
                if result.exists():
                    require(result.read_text().strip() == "0", "Update cancelled or installation failed. Try again.")
        except Exception as exc:
            self._set(
                status="error",
                message=str(exc) if isinstance(exc, UpdateError) else "Could not install the update. Try again.",
            )

    def close(self):
        self.cancelled.set()
