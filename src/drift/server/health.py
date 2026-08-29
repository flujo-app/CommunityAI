"""Bounded machine-readable health state for manifested public workers."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

MAX_HEALTH_STATE_BYTES = 4096
_UTC_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
_REQUIRED_ADMISSION_FIELDS = {
    "accepted_sessions",
    "active_session_routes",
    "active_sessions",
    "healthy",
    "pending_pushes",
    "rejected_sessions",
    "tracked_peers",
}


class HealthStateError(ValueError):
    """A public-worker health target or snapshot is unsafe."""


def validate_health_state_path(value: str | os.PathLike[str]) -> Path:
    text = os.fspath(value)
    if not text or "\x00" in text or "\r" in text or "\n" in text or len(text) > 512:
        raise HealthStateError("public health state path must be a bounded value")
    path = Path(text)
    if not path.is_absolute():
        raise HealthStateError("public health state path must be absolute")
    parent = path.parent
    try:
        if os.path.realpath(parent) != os.path.abspath(parent):
            raise HealthStateError("public health state directory must not traverse a symbolic link")
        parent_mode = parent.stat().st_mode
    except OSError as exc:
        raise HealthStateError("public health state directory is unavailable") from exc
    if not stat.S_ISDIR(parent_mode):
        raise HealthStateError("public health state parent must be a directory")
    try:
        target_mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise HealthStateError("public health state target is unavailable") from exc
    else:
        if stat.S_ISLNK(target_mode) or not stat.S_ISREG(target_mode):
            raise HealthStateError("public health state target must be a regular non-symlink file")
    return path


def _bounded_count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise HealthStateError(f"public health {field} is invalid")
    return value


def build_public_worker_health(
    *,
    manifest_digest: str,
    start_block: int,
    end_block: int,
    admission_snapshot: Mapping[str, Any] | None,
    ready: bool,
    announcer_alive: bool,
    handlers_alive: bool,
    pools_alive: bool,
    observed_at: str | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(manifest_digest, str)
        or len(manifest_digest) != 71
        or not manifest_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in manifest_digest[7:])
    ):
        raise HealthStateError("public health manifest digest is invalid")
    if (
        isinstance(start_block, bool)
        or isinstance(end_block, bool)
        or not isinstance(start_block, int)
        or not isinstance(end_block, int)
        or start_block < 0
        or end_block <= start_block
    ):
        raise HealthStateError("public health block range is invalid")
    for field, value in (
        ("ready", ready),
        ("announcer_alive", announcer_alive),
        ("handlers_alive", handlers_alive),
        ("pools_alive", pools_alive),
    ):
        if not isinstance(value, bool):
            raise HealthStateError(f"public health {field} must be boolean")

    admission_available = admission_snapshot is not None
    if admission_snapshot is None:
        admission = {
            "accepted_sessions": None,
            "active_session_routes": None,
            "active_sessions": None,
            "healthy": False,
            "pending_pushes": None,
            "rejected_sessions": None,
            "tracked_peers": None,
        }
    else:
        if set(admission_snapshot) != _REQUIRED_ADMISSION_FIELDS:
            raise HealthStateError("public admission health schema is invalid")
        admission = {
            field: (
                admission_snapshot[field]
                if field == "healthy"
                else _bounded_count(admission_snapshot[field], f"admission.{field}")
            )
            for field in sorted(_REQUIRED_ADMISSION_FIELDS)
        }
        if not isinstance(admission["healthy"], bool):
            raise HealthStateError("public admission health flag must be boolean")

    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(observed_at, str) or _UTC_TIMESTAMP_RE.fullmatch(observed_at) is None:
        raise HealthStateError("public health timestamp is invalid")
    try:
        parsed_observed_at = datetime.fromisoformat(observed_at[:-1] + "+00:00")
    except ValueError as exc:
        raise HealthStateError("public health timestamp is invalid") from exc
    if parsed_observed_at.tzinfo is None or parsed_observed_at.utcoffset() != timezone.utc.utcoffset(None):
        raise HealthStateError("public health timestamp is invalid")

    worker_healthy = (
        admission_available
        and admission["healthy"] is True
        and ready
        and announcer_alive
        and handlers_alive
        and pools_alive
    )
    return {
        "schema_version": 1,
        "scope": "manifested-public-worker-health",
        "observed_at": observed_at,
        "worker_healthy": worker_healthy,
        "route": {
            "manifest_digest": manifest_digest,
            "start_block": start_block,
            "end_block": end_block,
        },
        "admission_available": admission_available,
        "admission": admission,
        "components": {
            "ready": ready,
            "announcer_alive": announcer_alive,
            "handlers_alive": handlers_alive,
            "pools_alive": pools_alive,
        },
    }


def write_public_worker_health(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> None:
    target = validate_health_state_path(path)
    try:
        encoded = (json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HealthStateError("public health state is not canonical JSON") from exc
    if len(encoded) > MAX_HEALTH_STATE_BYTES:
        raise HealthStateError("public health state exceeds its bounded size")

    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        validate_health_state_path(target)
        os.replace(temporary, target)
        temporary = None
    except HealthStateError:
        raise
    except OSError as exc:
        raise HealthStateError("public health state could not be written") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
