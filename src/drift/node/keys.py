"""Local API-key persistence for the headless node bootstrap."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

API_KEY_STORE_SCHEMA_VERSION = 1
_KEY_HASH_DOMAIN = b"drift-local-api-key-v1\0"
_KEY_ID_RE = re.compile(r"^key_[0-9a-f]{16}$")


class ApiKeyStoreError(ValueError):
    pass


class ApiKeyNotFoundError(LookupError):
    pass


class LastActiveKeyError(RuntimeError):
    pass


def _key_hash(secret: str) -> str:
    return hashlib.sha256(_KEY_HASH_DOMAIN + secret.encode("utf-8")).hexdigest()


@dataclass
class _ApiKeyRecord:
    key_id: str
    label: str
    secret_hash: str
    created_at: int
    revoked_at: Optional[int] = None

    def metadata(self) -> Dict[str, Any]:
        return {
            "id": self.key_id,
            "label": self.label,
            "fingerprint": self.secret_hash[:12],
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
        }


class ApiKeyStore:
    """Persistent labeled bearer-key hashes with atomic mutation and revocation."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._records = self._load()

    def _load(self) -> Dict[str, _ApiKeyRecord]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or not self.path.is_file():
            raise ApiKeyStoreError(f"API key store path is not a regular file: {self.path}")

        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ApiKeyStoreError(f"API key store contains duplicate object key {key!r}")
                result[key] = value
            return result

        try:
            source = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ApiKeyStoreError(f"Could not read API key store {self.path}: {exc}") from exc
        if not isinstance(source, dict) or set(source) != {"schema_version", "keys"}:
            raise ApiKeyStoreError("API key store must contain exactly schema_version and keys")
        if (
            isinstance(source["schema_version"], bool)
            or not isinstance(source["schema_version"], int)
            or source["schema_version"] != API_KEY_STORE_SCHEMA_VERSION
        ):
            raise ApiKeyStoreError(f"Unsupported API key store schema_version {source['schema_version']!r}")
        if not isinstance(source["keys"], list):
            raise ApiKeyStoreError("API key store keys must be an array")

        records = {}
        for index, item in enumerate(source["keys"]):
            fields = {"id", "label", "secret_hash", "created_at", "revoked_at"}
            if not isinstance(item, dict) or set(item) != fields:
                raise ApiKeyStoreError(f"API key store keys[{index}] has invalid fields")
            key_id, label, secret_hash = item["id"], item["label"], item["secret_hash"]
            created_at, revoked_at = item["created_at"], item["revoked_at"]
            if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
                raise ApiKeyStoreError(f"API key store keys[{index}].id is invalid")
            if not isinstance(label, str) or not label or len(label) > 64:
                raise ApiKeyStoreError(f"API key store keys[{index}].label is invalid")
            if (
                not isinstance(secret_hash, str)
                or len(secret_hash) != 64
                or any(character not in "0123456789abcdef" for character in secret_hash)
            ):
                raise ApiKeyStoreError(f"API key store keys[{index}].secret_hash is invalid")
            if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at < 0:
                raise ApiKeyStoreError(f"API key store keys[{index}].created_at is invalid")
            if revoked_at is not None and (
                isinstance(revoked_at, bool) or not isinstance(revoked_at, int) or revoked_at < created_at
            ):
                raise ApiKeyStoreError(f"API key store keys[{index}].revoked_at is invalid")
            normalized = key_id.casefold()
            if normalized in records:
                raise ApiKeyStoreError(f"API key store contains duplicate id {key_id!r}")
            records[normalized] = _ApiKeyRecord(key_id, label, secret_hash, created_at, revoked_at)
        return records

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        document = {
            "schema_version": API_KEY_STORE_SCHEMA_VERSION,
            "keys": [
                {
                    "id": record.key_id,
                    "label": record.label,
                    "secret_hash": record.secret_hash,
                    "created_at": record.created_at,
                    "revoked_at": record.revoked_at,
                }
                for record in sorted(self._records.values(), key=lambda item: item.key_id)
            ],
        }
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_label(label: str) -> str:
        if not isinstance(label, str) or not label.strip() or len(label) > 64:
            raise ApiKeyStoreError("API key label must be a non-empty string of at most 64 characters")
        return label.strip()

    def ensure_key(self, secret: str, *, label: str) -> Dict[str, Any]:
        if not isinstance(secret, str) or not secret:
            raise ApiKeyStoreError("API key secret must not be empty")
        label = self._validate_label(label)
        secret_hash = _key_hash(secret)
        with self._lock:
            for record in self._records.values():
                if secrets.compare_digest(record.secret_hash, secret_hash):
                    if record.revoked_at is not None:
                        raise ApiKeyStoreError("a revoked API key cannot be imported again")
                    return record.metadata()
            record = _ApiKeyRecord(
                key_id=f"key_{secrets.token_hex(8)}",
                label=label,
                secret_hash=secret_hash,
                created_at=int(time.time()),
            )
            self._records[record.key_id.casefold()] = record
            try:
                self._write_locked()
            except BaseException:
                self._records.pop(record.key_id.casefold(), None)
                raise
            return record.metadata()

    def create(self, *, label: str) -> Tuple[Dict[str, Any], str]:
        secret = f"drift_{secrets.token_urlsafe(32)}"
        return self.ensure_key(secret, label=label), secret

    def verify(self, candidate: str) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        candidate_hash = _key_hash(candidate)
        with self._lock:
            matches = False
            for record in self._records.values():
                active_match = record.revoked_at is None and secrets.compare_digest(record.secret_hash, candidate_hash)
                matches = matches or active_match
            return matches

    def list(self) -> Tuple[Dict[str, Any], ...]:
        with self._lock:
            return tuple(record.metadata() for record in sorted(self._records.values(), key=lambda item: item.key_id))

    def update_label(self, key_id: str, *, label: str) -> Dict[str, Any]:
        label = self._validate_label(label)
        with self._lock:
            record = self._records.get(key_id.casefold())
            if record is None:
                raise ApiKeyNotFoundError(f"unknown API key {key_id!r}")
            previous_label = record.label
            record.label = label
            try:
                self._write_locked()
            except BaseException:
                record.label = previous_label
                raise
            return record.metadata()

    def revoke(self, key_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._records.get(key_id.casefold())
            if record is None:
                raise ApiKeyNotFoundError(f"unknown API key {key_id!r}")
            if record.revoked_at is not None:
                return record.metadata()
            if sum(item.revoked_at is None for item in self._records.values()) <= 1:
                raise LastActiveKeyError("cannot revoke the last active API key")
            record.revoked_at = int(time.time())
            try:
                self._write_locked()
            except BaseException:
                record.revoked_at = None
                raise
            return record.metadata()


def load_or_create_api_key(path: Path) -> Tuple[str, bool]:
    """Load a dedicated secret file or create it atomically with private permissions.

    Native credential-store integration belongs to the desktop milestone. This
    headless bootstrap keeps the secret separate from ordinary JSON/TOML config and
    never writes it to application logs.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    def read_existing() -> Tuple[str, bool]:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"API key path is not a regular file: {path}")
        stored = path.read_text(encoding="utf-8").strip()
        if not stored:
            raise ValueError(f"API key file is empty: {path}")
        return stored, False

    if path.exists() or path.is_symlink():
        return read_existing()

    key = f"drift_{secrets.token_urlsafe(32)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return read_existing()

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise

    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key, True
