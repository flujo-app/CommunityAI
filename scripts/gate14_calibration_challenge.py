"""Short-lived, source-bound calibration challenges for Gate 14 host probes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
SCOPE = "gate14-calibration-challenge"
MAX_JSON_BYTES = 16_384
MAX_LIFETIME_SECONDS = 900
MIN_LIFETIME_SECONDS = 60
MAX_SAMPLE_WINDOW_SECONDS = 120

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_NONCE_RE = re.compile(r"[0-9a-f]{64}")
_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "source_commit",
    "package_sha256",
    "checkpoint_sha256",
    "controller_state_revision",
    "issued_at_unix",
    "expires_at_unix",
    "nonce",
}


class Gate14ChallengeError(ValueError):
    """A calibration challenge is malformed, stale, or incorrectly bound."""


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14ChallengeError("duplicate challenge field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise Gate14ChallengeError("non-finite challenge value")


def canonical_payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_payload(value)).hexdigest()


def parse_payload(payload: bytes) -> Mapping[str, Any]:
    if not 1 <= len(payload) <= MAX_JSON_BYTES:
        raise Gate14ChallengeError("challenge size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14ChallengeError("challenge is invalid JSON") from exc
    if not isinstance(value, dict):
        raise Gate14ChallengeError("challenge must be an object")
    return value


def regular_payload(path: Path) -> bytes:
    path = Path(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise Gate14ChallengeError("challenge is unavailable") from exc
    reparse = bool(getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_JSON_BYTES:
        raise Gate14ChallengeError("challenge path is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or not 1 <= opened.st_size <= MAX_JSON_BYTES
            ):
                raise Gate14ChallengeError("challenge changed while opening")
            payload = handle.read(MAX_JSON_BYTES + 1)
            after = os.fstat(handle.fileno())
    except Gate14ChallengeError:
        raise
    except OSError as exc:
        raise Gate14ChallengeError("challenge is unreadable") from exc
    if len(payload) != opened.st_size or (after.st_dev, after.st_ino, after.st_size) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
    ):
        raise Gate14ChallengeError("challenge changed while reading")
    return payload


def load(path: Path) -> Mapping[str, Any]:
    return parse_payload(regular_payload(path))


def validate(
    value: Mapping[str, Any],
    *,
    run_id: str,
    platform: str,
    source_commit: str,
    package_sha256: str,
    checkpoint_sha256: str | None = None,
    now_unix: float | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise Gate14ChallengeError("challenge schema is invalid")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["scope"] != SCOPE
        or not isinstance(value["run_id"], str)
        or _RUN_RE.fullmatch(value["run_id"]) is None
        or value["run_id"] != run_id
        or value["platform"] not in {"windows", "linux"}
        or value["platform"] != platform
        or not isinstance(value["source_commit"], str)
        or _COMMIT_RE.fullmatch(value["source_commit"]) is None
        or value["source_commit"] != source_commit
        or not isinstance(value["package_sha256"], str)
        or _DIGEST_RE.fullmatch(value["package_sha256"]) is None
        or value["package_sha256"] != package_sha256
        or not isinstance(value["checkpoint_sha256"], str)
        or _DIGEST_RE.fullmatch(value["checkpoint_sha256"]) is None
        or (checkpoint_sha256 is not None and value["checkpoint_sha256"] != checkpoint_sha256)
        or not isinstance(value["nonce"], str)
        or _NONCE_RE.fullmatch(value["nonce"]) is None
    ):
        raise Gate14ChallengeError("challenge binding is invalid")
    revision = value["controller_state_revision"]
    issued = value["issued_at_unix"]
    expires = value["expires_at_unix"]
    if (
        type(revision) is not int
        or revision < 0
        or type(issued) is not int
        or type(expires) is not int
        or not MIN_LIFETIME_SECONDS <= expires - issued <= MAX_LIFETIME_SECONDS
    ):
        raise Gate14ChallengeError("challenge lifetime is invalid")
    if now_unix is not None:
        if type(now_unix) not in (int, float):
            raise Gate14ChallengeError("challenge clock is invalid")
        now = float(now_unix)
        if not issued <= now <= expires:
            raise Gate14ChallengeError("challenge is not currently valid")
    return dict(value)


def create(
    *,
    run_id: str,
    platform: str,
    source_commit: str,
    package_sha256: str,
    checkpoint_sha256: str,
    controller_state_revision: int,
    issued_at_unix: int | None = None,
    lifetime_seconds: int = MAX_LIFETIME_SECONDS,
    nonce: str | None = None,
) -> Mapping[str, Any]:
    issued = int(time.time()) if issued_at_unix is None else issued_at_unix
    value = {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": platform,
        "source_commit": source_commit,
        "package_sha256": package_sha256,
        "checkpoint_sha256": checkpoint_sha256,
        "controller_state_revision": controller_state_revision,
        "issued_at_unix": issued,
        "expires_at_unix": issued + lifetime_seconds,
        "nonce": secrets.token_hex(32) if nonce is None else nonce,
    }
    return validate(
        value,
        run_id=run_id,
        platform=platform,
        source_commit=source_commit,
        package_sha256=package_sha256,
        checkpoint_sha256=checkpoint_sha256,
        now_unix=issued,
    )


def write_new(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise Gate14ChallengeError("challenge output already exists")
    payload = canonical_payload(value) + os.linesep.encode("ascii")
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise Gate14ChallengeError("challenge output already exists") from exc
        temporary.unlink()
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
