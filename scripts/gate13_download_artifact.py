#!/usr/bin/env python3
"""Fetch one exact GitHub Actions package without exposing its signed URL."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import BinaryIO

_ALLOWED_CONFIG_NAMES = frozenset(
    {
        "gate13_download_linux.json",
        "gate13_download_windows.json",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "allowed_host",
        "archive_bytes",
        "archive_name",
        "archive_sha256",
        "schema_version",
        "wrapper_bytes",
    }
)
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CONFIG_BYTES = 4096
_MAX_URL_BYTES = 8192
_MAX_WRAPPER_OVERHEAD = 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


class Gate13DownloadError(RuntimeError):
    """A deliberately detail-free qualification download failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise Gate13DownloadError


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Gate13DownloadError
        result[key] = value
    return result


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_archive_name(value: object) -> str:
    if not isinstance(value, str) or not _ARCHIVE_NAME.fullmatch(value):
        raise Gate13DownloadError
    if value in {".", ".."} or value[-1] in {".", " "}:
        raise Gate13DownloadError
    stem = value.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"}:
        raise Gate13DownloadError
    if re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
        raise Gate13DownloadError
    return value


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise Gate13DownloadError from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise Gate13DownloadError
    if before.st_size > maximum_bytes:
        raise Gate13DownloadError

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Gate13DownloadError from exc
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode):
            raise Gate13DownloadError
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise Gate13DownloadError
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(maximum_bytes + 1)
    finally:
        os.close(descriptor)
    if len(data) > maximum_bytes:
        raise Gate13DownloadError
    return data


def _load_config(base_dir: Path, config_name: str) -> dict[str, object]:
    if config_name not in _ALLOWED_CONFIG_NAMES:
        raise Gate13DownloadError
    config_path = base_dir / config_name
    try:
        raw = _read_regular_file(config_path, _MAX_CONFIG_BYTES)
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except Gate13DownloadError:
        raise
    except Exception as exc:
        raise Gate13DownloadError from exc

    if not isinstance(config, dict) or frozenset(config) != _CONFIG_KEYS:
        raise Gate13DownloadError
    if config["schema_version"] != 1 or not _is_strict_int(config["schema_version"]):
        raise Gate13DownloadError

    allowed_host = config["allowed_host"]
    if (
        not isinstance(allowed_host, str)
        or allowed_host != allowed_host.lower()
        or not re.fullmatch(r"[a-z0-9.-]{1,253}", allowed_host)
        or not allowed_host.endswith(".blob.core.windows.net")
    ):
        raise Gate13DownloadError

    archive_name = _validate_archive_name(config["archive_name"])
    archive_bytes = config["archive_bytes"]
    wrapper_bytes = config["wrapper_bytes"]
    if not _is_strict_int(archive_bytes) or not _is_strict_int(wrapper_bytes):
        raise Gate13DownloadError
    if archive_bytes <= 0 or wrapper_bytes < archive_bytes:
        raise Gate13DownloadError
    if wrapper_bytes > archive_bytes + _MAX_WRAPPER_OVERHEAD:
        raise Gate13DownloadError

    archive_sha256 = config["archive_sha256"]
    if not isinstance(archive_sha256, str) or not _SHA256.fullmatch(archive_sha256):
        raise Gate13DownloadError

    return {
        "allowed_host": allowed_host,
        "archive_bytes": archive_bytes,
        "archive_name": archive_name,
        "archive_sha256": archive_sha256,
        "schema_version": 1,
        "wrapper_bytes": wrapper_bytes,
    }


def _read_signed_url(stream: BinaryIO) -> str:
    raw = stream.read(_MAX_URL_BYTES + 1)
    if len(raw) > _MAX_URL_BYTES or not raw:
        raise Gate13DownloadError
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    if not raw or b"\r" in raw or b"\n" in raw or b"\x00" in raw:
        raise Gate13DownloadError
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise Gate13DownloadError from exc


def _validate_signed_url(url: str, allowed_host: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise Gate13DownloadError from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != allowed_host
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or not parsed.query
        or parsed.fragment
    ):
        raise Gate13DownloadError


def _open_exclusive(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise Gate13DownloadError from exc
    return os.fdopen(descriptor, "wb")


def _assert_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise Gate13DownloadError from exc
    raise Gate13DownloadError


def _copy_exact(source: BinaryIO, destination: BinaryIO, expected_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while True:
        request_bytes = min(_CHUNK_BYTES, expected_bytes - total + 1)
        chunk = source.read(request_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > expected_bytes:
            raise Gate13DownloadError
        destination.write(chunk)
        digest.update(chunk)
    if total != expected_bytes:
        raise Gate13DownloadError
    destination.flush()
    os.fsync(destination.fileno())
    return total, digest.hexdigest()


def _download_wrapper(url: str, target: Path, expected_bytes: int, opener) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "CommunityAI-Gate13/1"})
    try:
        with opener.open(request, timeout=60) as response:
            if getattr(response, "status", None) != 200:
                raise Gate13DownloadError
            if response.geturl() != url:
                raise Gate13DownloadError
            if response.headers.get("Content-Length") != str(expected_bytes):
                raise Gate13DownloadError
            with _open_exclusive(target) as destination:
                total, _ = _copy_exact(response, destination, expected_bytes)
            if total != expected_bytes:
                raise Gate13DownloadError
    except Gate13DownloadError:
        raise
    except Exception as exc:
        raise Gate13DownloadError from exc


def _extract_inner_archive(wrapper: Path, target: Path, config: dict[str, object]) -> tuple[int, str]:
    expected_name = str(config["archive_name"])
    expected_bytes = int(config["archive_bytes"])
    expected_sha256 = str(config["archive_sha256"])
    try:
        with zipfile.ZipFile(wrapper, mode="r", allowZip64=True) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise Gate13DownloadError
            member = members[0]
            unix_file_type = 0
            if member.create_system == 3:
                unix_file_type = stat.S_IFMT(member.external_attr >> 16)
            if (
                member.filename != expected_name
                or member.is_dir()
                or member.file_size != expected_bytes
                or member.compress_type != zipfile.ZIP_STORED
                or member.flag_bits & 0x1
                or unix_file_type not in {0, stat.S_IFREG}
            ):
                raise Gate13DownloadError
            with archive.open(member, mode="r") as source, _open_exclusive(target) as destination:
                total, digest = _copy_exact(source, destination, expected_bytes)
    except Gate13DownloadError:
        raise
    except Exception as exc:
        raise Gate13DownloadError from exc
    if digest != expected_sha256:
        raise Gate13DownloadError
    return total, digest


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise Gate13DownloadError from exc


def _same_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        status = path.lstat()
    except OSError:
        return False
    return not stat.S_ISLNK(status.st_mode) and (status.st_dev, status.st_ino) == identity


def _commit_no_replace(partial: Path, target: Path) -> None:
    try:
        partial_status = partial.lstat()
    except OSError as exc:
        raise Gate13DownloadError from exc
    if stat.S_ISLNK(partial_status.st_mode) or not stat.S_ISREG(partial_status.st_mode):
        raise Gate13DownloadError

    identity = (partial_status.st_dev, partial_status.st_ino)
    linked = False
    try:
        os.link(partial, target)
        linked = True
        partial.unlink()
        if os.name != "nt":
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception as exc:
        if linked and _same_identity(target, identity):
            try:
                target.unlink()
            except OSError:
                pass
        raise Gate13DownloadError from exc


def download(base_dir: Path, config_name: str, stream: BinaryIO, *, opener=None) -> dict[str, object]:
    base_dir = base_dir.resolve(strict=True)
    config = _load_config(base_dir, config_name)
    archive_name = str(config["archive_name"])
    wrapper = base_dir / ("." + archive_name + ".wrapper.partial")
    partial = base_dir / ("." + archive_name + ".partial")
    target = base_dir / archive_name

    for path in (wrapper, partial, target):
        _assert_absent(path)

    url = _read_signed_url(stream)
    _validate_signed_url(url, str(config["allowed_host"]))
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    try:
        _download_wrapper(url, wrapper, int(config["wrapper_bytes"]), opener)
        total, digest = _extract_inner_archive(wrapper, partial, config)
        _remove(wrapper)
        _commit_no_replace(partial, target)
    finally:
        url = ""
        _remove(wrapper)
        _remove(partial)

    return {
        "archive_bytes": total,
        "archive_sha256": digest,
        "clean": True,
        "result": "passed",
        "url_retained": False,
    }


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise Gate13DownloadError
        result = download(Path(__file__).resolve(strict=True).parent, sys.argv[1], sys.stdin.buffer)
    except Exception:
        sys.stdout.write('{"result":"failed","url_retained":false}\n')
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
