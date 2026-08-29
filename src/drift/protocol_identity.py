"""Cryptographic identities and signed public-swarm protocol records.

Public workers reuse their persistent libp2p RSA identity.  A worker announcement is
therefore signed by the private key whose public key derives the PeerID that libp2p
authenticates on the TLS 1.3 connection.  The DHT remains an untrusted transport: a
record is accepted only after its signature, identity binding, lifetime, manifest,
and replay ordering have been checked locally.

The signed-record envelope is intentionally independent of msgpack and Hivemind's
internal DHT validators.  DHT replicas do not need a private key or custom validator,
and every client can validate records received from old or malicious replicas.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import multihash
from cryptography import exceptions
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from hivemind.p2p import PeerID
from hivemind.p2p.p2p_daemon import P2P
from hivemind.proto import crypto_pb2

SIGNED_RECORD_SCHEMA_VERSION = 1
SIGNED_RECORD_DOMAIN = b"drift-signed-record-v1\x00"
SIGNATURE_ALGORITHM = "rsa-pss-sha256"
TRANSPORT_SECURITY = "libp2p-tls1.3"
MAX_SIGNED_RECORD_TTL_SECONDS = 60 * 60
MAX_CLOCK_SKEW_SECONDS = 60
REPLAY_HISTORY_SCHEMA_VERSION = 1
MAX_REPLAY_HISTORY_BYTES = 256 * 1024
MAX_REPLAY_HISTORY_ENTRIES = 256

_RSA_PADDING = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH)
_RSA_HASH = hashes.SHA256()


class ProtocolSecurityError(ValueError):
    """A public-swarm identity or signed record failed closed."""


def _strict_fields(value: Mapping[str, Any], fields: Iterable[str], *, name: str) -> None:
    expected = set(fields)
    actual = set(value)
    missing, unknown = expected - actual, actual - expected
    if missing:
        raise ProtocolSecurityError(f"{name} is missing required fields: {sorted(missing)}")
    if unknown:
        raise ProtocolSecurityError(f"{name} contains unknown fields: {sorted(unknown)}")


def _require_int(value: Any, *, name: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolSecurityError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ProtocolSecurityError(f"{name} must be at least {minimum}")
    return value


def _require_text(value: Any, *, name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ProtocolSecurityError(f"{name} must be a{' non-empty' if not allow_empty else ''} string")
    if unicodedata.normalize("NFC", value) != value:
        raise ProtocolSecurityError(f"{name} must be NFC-normalized")
    return value


def _require_digest(value: Any, *, name: str) -> str:
    value = _require_text(value, name=name)
    if len(value) != 64 or value.lower() != value or any(char not in "0123456789abcdef" for char in value):
        raise ProtocolSecurityError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_key_id(value: Any, *, name: str) -> str:
    value = _require_text(value, name=name)
    if not value.startswith("sha256:"):
        raise ProtocolSecurityError(f"{name} must be a sha256: key identifier")
    _require_digest(value.removeprefix("sha256:"), name=name)
    return value


def _normalize_json(value: Any, *, path: str = "value") -> Any:
    """Return the one JSON representation allowed in signed payloads."""
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and unicodedata.normalize("NFC", value) != value:
            raise ProtocolSecurityError(f"{path} must contain only NFC-normalized strings")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolSecurityError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, path=f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ProtocolSecurityError(f"{path} contains a non-string or empty object key")
            _require_text(key, name=f"{path} key")
            result[key] = _normalize_json(item, path=f"{path}.{key}")
        return result
    raise ProtocolSecurityError(f"{path} contains unsupported value {type(value).__name__}")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        _normalize_json(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _b64decode(value: Any, *, name: str) -> bytes:
    value = _require_text(value, name=name)
    try:
        decoded = base64.b64decode(value, validate=True)
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ProtocolSecurityError(f"{name} is not canonical base64")
        return decoded
    except (ValueError, TypeError) as exc:
        raise ProtocolSecurityError(f"{name} is not canonical base64") from exc


def _public_key_to_peer_id(public_key_der: bytes) -> PeerID:
    encoded = crypto_pb2.PublicKey(key_type=crypto_pb2.RSA, data=public_key_der).SerializeToString()
    digest = multihash.encode(hashlib.sha256(encoded).digest(), multihash.coerce_code("sha2-256"))
    return PeerID(digest)


def _load_public_key(public_key_der: bytes) -> rsa.RSAPublicKey:
    try:
        key = serialization.load_der_public_key(public_key_der)
    except (TypeError, ValueError) as exc:
        raise ProtocolSecurityError("signed record contains an invalid DER public key") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ProtocolSecurityError("signed record requires an RSA public key of at least 2048 bits")
    return key


@dataclass(frozen=True)
class NodeIdentity:
    """A persistent libp2p identity usable for both PeerID and record signatures."""

    _private_key: rsa.RSAPrivateKey = field(repr=False)
    identity_bytes: bytes = field(repr=False)

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> "NodeIdentity":
        path = Path(path)
        try:
            identity_bytes = path.read_bytes()
            protobuf = crypto_pb2.PrivateKey.FromString(identity_bytes)
            private_key = serialization.load_der_private_key(protobuf.data, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise ProtocolSecurityError(f"Could not load libp2p identity {path}: {exc}") from exc
        if protobuf.key_type != crypto_pb2.RSA or not isinstance(private_key, rsa.RSAPrivateKey):
            raise ProtocolSecurityError("Public-swarm identities must use RSA")
        if private_key.key_size < 2048:
            raise ProtocolSecurityError("Public-swarm identities must use RSA keys of at least 2048 bits")
        return cls(private_key, identity_bytes)

    @classmethod
    def create(cls, path: os.PathLike[str] | str, *, overwrite: bool = False) -> "NodeIdentity":
        path = Path(path)
        if path.exists() and not overwrite:
            raise ProtocolSecurityError(f"Identity already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        P2P.generate_identity(str(path))
        return cls.load(path)

    @classmethod
    def ensure(cls, path: os.PathLike[str] | str) -> "NodeIdentity":
        path = Path(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            P2P.generate_identity(str(path))
        return cls.load(path)

    @property
    def public_key_der(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self.public_key_der).decode("ascii")

    @property
    def key_id(self) -> str:
        return f"sha256:{hashlib.sha256(self.public_key_der).hexdigest()}"

    @property
    def peer_id(self) -> PeerID:
        # Keep this assertion tied to Hivemind's own implementation of the libp2p spec.
        derived = _public_key_to_peer_id(self.public_key_der)
        hivemind_derived = PeerID.from_identity(self.identity_bytes)
        if derived != hivemind_derived:  # pragma: no cover - defensive dependency compatibility check
            raise ProtocolSecurityError("Public key does not derive the libp2p identity's PeerID")
        return derived

    def sign(self, data: bytes) -> bytes:
        return self._private_key.sign(data, _RSA_PADDING, _RSA_HASH)


@dataclass(frozen=True)
class SignedRecord:
    schema_version: int
    kind: str
    algorithm: str
    key_id: str
    public_key: str
    payload: Dict[str, Any]
    signature: str

    @classmethod
    def create(cls, kind: str, payload: Mapping[str, Any], identity: NodeIdentity) -> "SignedRecord":
        record = cls(
            schema_version=SIGNED_RECORD_SCHEMA_VERSION,
            kind=_require_text(kind, name="signed record kind"),
            algorithm=SIGNATURE_ALGORITHM,
            key_id=identity.key_id,
            public_key=identity.public_key_b64,
            payload=_normalize_json(payload, path="signed record payload"),
            signature="",
        )
        signature = base64.b64encode(identity.sign(record.signing_bytes())).decode("ascii")
        return cls(**{**record.__dict__, "signature": signature})

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "SignedRecord":
        if not isinstance(source, Mapping):
            raise ProtocolSecurityError("signed record must be an object")
        fields = ("schema_version", "kind", "algorithm", "key_id", "public_key", "payload", "signature")
        _strict_fields(source, fields, name="signed record")
        _require_int(source["schema_version"], name="signed record schema_version", minimum=1)
        if source["schema_version"] != SIGNED_RECORD_SCHEMA_VERSION:
            raise ProtocolSecurityError(f"Unsupported signed record schema version {source['schema_version']!r}")
        _require_text(source["kind"], name="signed record kind")
        if source["algorithm"] != SIGNATURE_ALGORITHM:
            raise ProtocolSecurityError(f"Unsupported signature algorithm {source['algorithm']!r}")
        _require_text(source["key_id"], name="signed record key_id")
        _b64decode(source["public_key"], name="signed record public_key")
        if not isinstance(source["payload"], Mapping):
            raise ProtocolSecurityError("signed record payload must be an object")
        _b64decode(source["signature"], name="signed record signature")
        return cls(
            schema_version=source["schema_version"],
            kind=source["kind"],
            algorithm=source["algorithm"],
            key_id=source["key_id"],
            public_key=source["public_key"],
            payload=_normalize_json(source["payload"], path="signed record payload"),
            signature=source["signature"],
        )

    def unsigned_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "public_key": self.public_key,
            "payload": self.payload,
        }

    def signing_bytes(self) -> bytes:
        return SIGNED_RECORD_DOMAIN + _canonical_json(self.unsigned_dict()).encode("utf-8")

    def to_dict(self) -> Dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}

    @property
    def public_key_der(self) -> bytes:
        return _b64decode(self.public_key, name="signed record public_key")

    @property
    def peer_id(self) -> PeerID:
        return _public_key_to_peer_id(self.public_key_der)

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    def verify(self, *, expected_kind: Optional[str] = None) -> None:
        if expected_kind is not None and self.kind != expected_kind:
            raise ProtocolSecurityError(f"Expected signed record kind {expected_kind!r}, got {self.kind!r}")
        public_der = self.public_key_der
        expected_key_id = f"sha256:{hashlib.sha256(public_der).hexdigest()}"
        if self.key_id != expected_key_id:
            raise ProtocolSecurityError("signed record key_id does not match its public key")
        public_key = _load_public_key(public_der)
        signature = _b64decode(self.signature, name="signed record signature")
        try:
            public_key.verify(signature, self.signing_bytes(), _RSA_PADDING, _RSA_HASH)
        except exceptions.InvalidSignature as exc:
            raise ProtocolSecurityError("signed record has an invalid signature") from exc


def _validate_lifetime(payload: Mapping[str, Any], *, now: Optional[float]) -> Tuple[int, int]:
    issued_at_ms = _require_int(payload.get("issued_at_ms"), name="issued_at_ms", minimum=0)
    expires_at_ms = _require_int(payload.get("expires_at_ms"), name="expires_at_ms", minimum=0)
    if expires_at_ms <= issued_at_ms:
        raise ProtocolSecurityError("signed record must expire after it was issued")
    ttl_ms = expires_at_ms - issued_at_ms
    if ttl_ms > MAX_SIGNED_RECORD_TTL_SECONDS * 1000:
        raise ProtocolSecurityError("signed record lifetime exceeds the public-swarm maximum")
    current_ms = int((time.time() if now is None else now) * 1000)
    if issued_at_ms > current_ms + MAX_CLOCK_SKEW_SECONDS * 1000:
        raise ProtocolSecurityError("signed record was issued too far in the future")
    if expires_at_ms <= current_ms:
        raise ProtocolSecurityError("signed record has expired")
    return issued_at_ms, expires_at_ms


@dataclass
class ReplayGuard:
    """Reject older records and optionally preserve the live ordering window across restarts."""

    path: Optional[Path | str] = None
    max_entries: int = MAX_REPLAY_HISTORY_ENTRIES
    clock: Callable[[], float] = time.time
    _latest: Dict[Tuple[str, str], Tuple[int, int, str, int]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.max_entries, bool) or not isinstance(self.max_entries, int) or self.max_entries <= 0:
            raise ValueError("replay history entry limit must be a positive integer")
        if self.path is None:
            return
        self.path = Path(os.path.abspath(os.fspath(Path(self.path).expanduser())))
        self._reload()

    def _reload(self) -> None:
        try:
            self._latest = self._load()
        except ProtocolSecurityError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise ProtocolSecurityError("replay history could not be loaded safely") from exc

    def __getstate__(self) -> Dict[str, Any]:
        """Serialize state across Hivemind's DHT process boundary without the thread lock."""

        with self._lock:
            state = dict(self.__dict__)
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: Mapping[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()
        # The DHT subprocess may receive an older parent snapshot after another
        # call advanced the on-disk watermark, so persistent guards reload it.
        if self.path is not None:
            self._reload()

    @staticmethod
    def _strict_json(source: str) -> Mapping[str, Any]:
        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ProtocolSecurityError(f"replay history contains duplicate key {key!r}")
                result[key] = value
            return result

        def reject_non_finite(value):
            raise ProtocolSecurityError(f"replay history contains non-finite number {value}")

        value = json.loads(source, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
        if not isinstance(value, dict):
            raise ProtocolSecurityError("replay history must be a JSON object")
        return value

    def _load(self) -> Dict[Tuple[str, str], Tuple[int, int, str, int]]:
        if self.path is None:
            return {}
        if self.path.parent.exists() and (self.path.parent.is_symlink() or not self.path.parent.is_dir()):
            raise ProtocolSecurityError("replay history directory is not a regular directory")
        if self.path.is_symlink():
            raise ProtocolSecurityError("replay history path is not a regular file")
        if not self.path.exists():
            return {}
        if not self.path.is_file():
            raise ProtocolSecurityError("replay history path is not a regular file")
        if self.path.stat().st_size > MAX_REPLAY_HISTORY_BYTES:
            raise ProtocolSecurityError("replay history exceeds its byte limit")
        source = self._strict_json(self.path.read_text(encoding="utf-8"))
        schema_version = source.get("schema_version")
        if (
            set(source) != {"schema_version", "entries"}
            or isinstance(schema_version, bool)
            or schema_version != REPLAY_HISTORY_SCHEMA_VERSION
        ):
            raise ProtocolSecurityError("replay history schema is invalid")
        entries = source["entries"]
        if not isinstance(entries, list) or len(entries) > self.max_entries:
            raise ProtocolSecurityError("replay history entry list is invalid")

        latest: Dict[Tuple[str, str], Tuple[int, int, str, int]] = {}
        now_ms = int(self.clock() * 1000)
        fields = {"kind", "key_id", "issued_at_ms", "sequence", "record_digest", "retain_until_ms"}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != fields:
                raise ProtocolSecurityError("replay history entry schema is invalid")
            kind = _require_text(entry["kind"], name="replay history kind")
            if len(kind) > 64:
                raise ProtocolSecurityError("replay history kind is too long")
            key_id = _require_key_id(entry["key_id"], name="replay history key_id")
            issued_at_ms = _require_int(entry["issued_at_ms"], name="issued_at_ms", minimum=0)
            sequence = _require_int(entry["sequence"], name="sequence", minimum=0)
            digest = _require_digest(entry["record_digest"], name="record_digest")
            retain_until_ms = _require_int(entry["retain_until_ms"], name="retain_until_ms", minimum=0)
            if retain_until_ms <= issued_at_ms:
                raise ProtocolSecurityError("replay history entry retention ends before it was issued")
            replay_scope = (kind, key_id)
            if replay_scope in latest:
                raise ProtocolSecurityError("replay history contains a duplicate identity scope")
            if retain_until_ms > now_ms:
                latest[replay_scope] = (issued_at_ms, sequence, digest, retain_until_ms)
        return latest

    def _persist(self, latest: Mapping[Tuple[str, str], Tuple[int, int, str, int]]) -> None:
        if self.path is None:
            return
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ProtocolSecurityError("replay history path is not a regular file")
        entries = [
            {
                "kind": kind,
                "key_id": key_id,
                "issued_at_ms": item[0],
                "sequence": item[1],
                "record_digest": item[2],
                "retain_until_ms": item[3],
            }
            for (kind, key_id), item in sorted(latest.items())
        ]
        rendered = (
            json.dumps(
                {"schema_version": REPLAY_HISTORY_SCHEMA_VERSION, "entries": entries},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        if len(rendered.encode("utf-8")) > MAX_REPLAY_HISTORY_BYTES:
            raise ProtocolSecurityError("rendered replay history exceeds its byte limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise ProtocolSecurityError("replay history directory is not a regular directory")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def check(self, record: SignedRecord) -> None:
        payload = record.payload
        issued_at_ms = _require_int(payload.get("issued_at_ms"), name="issued_at_ms", minimum=0)
        expires_at_ms = _require_int(payload.get("expires_at_ms"), name="expires_at_ms", minimum=0)
        sequence = _require_int(payload.get("sequence"), name="sequence", minimum=0)
        replay_scope = (record.kind, record.key_id)
        order = (issued_at_ms, sequence)
        maximum_ttl_seconds = (
            ROUTE_DEMAND_MAX_TTL_SECONDS if record.kind == "route_demand" else MAX_SIGNED_RECORD_TTL_SECONDS
        )
        retain_until_ms = max(expires_at_ms, issued_at_ms + maximum_ttl_seconds * 1000)
        with self._lock:
            now_ms = int(self.clock() * 1000)
            latest = {key: item for key, item in self._latest.items() if item[3] > now_ms}
            current = latest.get(replay_scope)
            if current is not None:
                previous_order = current[:2]
                if order < previous_order:
                    raise ProtocolSecurityError(
                        "signed record is older than a record already observed for this identity"
                    )
                if order == previous_order and record.digest != current[2]:
                    raise ProtocolSecurityError("identity equivocated by signing different records at one sequence")
            changed = latest != self._latest
            if current is None or order > current[:2]:
                latest[replay_scope] = (issued_at_ms, sequence, record.digest, retain_until_ms)
                changed = True
            if len(latest) > self.max_entries:
                raise ProtocolSecurityError("replay history reached its active entry limit")
            if changed:
                try:
                    self._persist(latest)
                except ProtocolSecurityError:
                    raise
                except (OSError, UnicodeError, TypeError, ValueError) as exc:
                    raise ProtocolSecurityError("replay history could not be persisted safely") from exc
                self._latest = latest


@dataclass
class RevocationStore:
    revoked_key_ids: set[str] = field(default_factory=set)
    successors: Dict[str, str] = field(default_factory=dict)
    peer_ids: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_files(cls, paths: Sequence[os.PathLike[str] | str]) -> "RevocationStore":
        documents = []
        for path in paths:
            try:

                def reject_duplicate_keys(pairs):
                    result = {}
                    for key, item in pairs:
                        if key in result:
                            raise ProtocolSecurityError(f"Trust record contains duplicate object key {key!r}")
                        result[key] = item
                    return result

                def reject_non_finite(value):
                    raise ProtocolSecurityError(f"Trust record contains non-finite number {value}")

                value = json.loads(
                    Path(path).read_text(encoding="utf-8"),
                    object_pairs_hook=reject_duplicate_keys,
                    parse_constant=reject_non_finite,
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ProtocolSecurityError(f"Could not read identity trust record {path}: {exc}") from exc
            documents.extend(value if isinstance(value, list) else [value])
        return cls.from_records(documents)

    @classmethod
    def from_records(cls, records: Sequence[Mapping[str, Any]]) -> "RevocationStore":
        store = cls()
        rotations = [item for item in records if isinstance(item, Mapping) and item.get("kind") == "identity_rotation"]
        revocations = [
            item for item in records if isinstance(item, Mapping) and item.get("kind") == "identity_revocation"
        ]
        unknown = [
            item
            for item in records
            if not isinstance(item, Mapping) or item.get("kind") not in {"identity_rotation", "identity_revocation"}
        ]
        if unknown:
            raise ProtocolSecurityError("Trust bundle contains an unknown identity record")
        for rotation in rotations:
            old_key_id, new_key_id = verify_rotation_record(rotation)
            if old_key_id in store.successors and store.successors[old_key_id] != new_key_id:
                raise ProtocolSecurityError("identity rotation forks to multiple successor keys")
            store.successors[old_key_id] = new_key_id
            store.peer_ids[old_key_id] = rotation["payload"]["old_peer_id"]
            store.peer_ids[new_key_id] = rotation["payload"]["new_peer_id"]
        for predecessor in store.successors:
            seen = set()
            current = predecessor
            while current in store.successors:
                if current in seen:
                    raise ProtocolSecurityError("identity rotation chain contains a cycle")
                seen.add(current)
                current = store.successors[current]
        for revocation in revocations:
            record = SignedRecord.from_dict(revocation)
            record.verify(expected_kind="identity_revocation")
            fields = ("authority_key_id", "revoked_key_id", "revoked_peer_id", "issued_at_ms", "sequence", "reason")
            _strict_fields(record.payload, fields, name="identity revocation payload")
            authority = _require_key_id(record.payload["authority_key_id"], name="authority_key_id")
            revoked = _require_key_id(record.payload["revoked_key_id"], name="revoked_key_id")
            revoked_peer_id = _require_text(record.payload["revoked_peer_id"], name="revoked_peer_id")
            if authority != record.key_id:
                raise ProtocolSecurityError("revocation authority does not match its signing key")
            _require_int(record.payload["issued_at_ms"], name="issued_at_ms", minimum=0)
            _require_int(record.payload["sequence"], name="sequence", minimum=0)
            _require_text(record.payload["reason"], name="reason", allow_empty=True)
            if authority != revoked and not store._is_successor(authority, revoked):
                raise ProtocolSecurityError("revocation is not signed by the revoked key or a proven successor")
            expected_peer_id = record.peer_id.to_base58() if authority == revoked else store.peer_ids.get(revoked)
            if expected_peer_id is not None and revoked_peer_id != expected_peer_id:
                raise ProtocolSecurityError("revocation names the wrong PeerID for the revoked key")
            store.revoked_key_ids.add(revoked)
        return store

    def _is_successor(self, candidate: str, predecessor: str) -> bool:
        seen = set()
        current = predecessor
        while current in self.successors and current not in seen:
            seen.add(current)
            current = self.successors[current]
            if current == candidate:
                return True
        return False

    def require_active(self, key_id: str) -> None:
        if key_id in self.revoked_key_ids:
            raise ProtocolSecurityError(f"identity {key_id} is revoked")


def create_worker_announcement(
    identity: NodeIdentity,
    *,
    dht_prefix: str,
    manifest_digest: str,
    execution_profile: Mapping[str, Any],
    server_info: Mapping[str, Any],
    issued_at: float,
    expires_at: float,
    sequence: int,
) -> SignedRecord:
    _require_digest(manifest_digest, name="manifest_digest")
    payload = {
        "peer_id": identity.peer_id.to_base58(),
        "dht_prefix": _require_text(dht_prefix, name="dht_prefix"),
        "manifest_digest": manifest_digest,
        "execution_profile": _normalize_json(execution_profile, path="execution_profile"),
        "server_info": _normalize_json(server_info, path="server_info"),
        "transport_security": TRANSPORT_SECURITY,
        "issued_at_ms": int(issued_at * 1000),
        "expires_at_ms": int(expires_at * 1000),
        "sequence": _require_int(sequence, name="sequence", minimum=0),
    }
    _validate_lifetime(payload, now=issued_at)
    return SignedRecord.create("worker_announcement", payload, identity)


def verify_worker_announcement(
    source: Mapping[str, Any],
    *,
    expected_peer_id: PeerID,
    expected_dht_prefix: str,
    expected_manifest_digest: str,
    expected_server_info: Mapping[str, Any],
    expected_execution_profile: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    revocations: Optional[RevocationStore] = None,
    replay_guard: Optional[ReplayGuard] = None,
) -> SignedRecord:
    record = SignedRecord.from_dict(source)
    record.verify(expected_kind="worker_announcement")
    fields = (
        "peer_id",
        "dht_prefix",
        "manifest_digest",
        "execution_profile",
        "server_info",
        "transport_security",
        "issued_at_ms",
        "expires_at_ms",
        "sequence",
    )
    _strict_fields(record.payload, fields, name="worker announcement payload")
    if record.peer_id != expected_peer_id or record.payload["peer_id"] != expected_peer_id.to_base58():
        raise ProtocolSecurityError("worker announcement public key does not derive its DHT PeerID")
    if record.payload["dht_prefix"] != expected_dht_prefix:
        raise ProtocolSecurityError("worker announcement is bound to a different DHT namespace")
    if expected_dht_prefix != f"drift-m1-{expected_manifest_digest}":
        raise ProtocolSecurityError("manifested announcement namespace is not derived from its manifest digest")
    if record.payload["manifest_digest"] != expected_manifest_digest:
        raise ProtocolSecurityError("worker announcement is bound to a different manifest")
    _require_digest(record.payload["manifest_digest"], name="manifest_digest")
    if record.payload["transport_security"] != TRANSPORT_SECURITY:
        raise ProtocolSecurityError("worker announcement does not require authenticated encrypted transport")
    if record.payload["server_info"] != _normalize_json(expected_server_info, path="server_info"):
        raise ProtocolSecurityError("worker announcement does not cover the published server metadata")
    if not isinstance(record.payload["execution_profile"], Mapping):
        raise ProtocolSecurityError("worker announcement execution_profile must be an object")
    if expected_execution_profile is not None and record.payload["execution_profile"] != _normalize_json(
        expected_execution_profile, path="execution_profile"
    ):
        raise ProtocolSecurityError("worker announcement execution profile does not match the manifest")
    _validate_lifetime(record.payload, now=now)
    _require_int(record.payload["sequence"], name="sequence", minimum=0)
    if revocations is not None:
        revocations.require_active(record.key_id)
    if replay_guard is not None:
        replay_guard.check(record)
    return record


INTENT_RESOURCE_CLAIMS_SCHEMA_VERSION = 1


def _validate_intent_resource_claims(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolSecurityError("intent lease resource_claims must be an object")
    fields = ("schema_version", "artifact_bytes", "block_count", "throughput_milli_rps")
    _strict_fields(value, fields, name="intent lease resource_claims")
    schema_version = _require_int(value["schema_version"], name="resource_claims.schema_version", minimum=1)
    if schema_version != INTENT_RESOURCE_CLAIMS_SCHEMA_VERSION:
        raise ProtocolSecurityError(f"unsupported intent resource claims schema version {schema_version}")
    artifact_bytes = _require_int(value["artifact_bytes"], name="resource_claims.artifact_bytes", minimum=0)
    block_count = _require_int(value["block_count"], name="resource_claims.block_count", minimum=1)
    throughput = value["throughput_milli_rps"]
    if throughput is not None:
        throughput = _require_int(throughput, name="resource_claims.throughput_milli_rps", minimum=1)
    return {
        "schema_version": schema_version,
        "artifact_bytes": artifact_bytes,
        "block_count": block_count,
        "throughput_milli_rps": throughput,
    }


def create_intent_lease(
    identity: NodeIdentity,
    *,
    manifest_digest: str,
    start_block: int,
    end_block: int,
    resource_claims: Mapping[str, Any],
    issued_at: float,
    expires_at: float,
    sequence: int,
    nonce: Optional[str] = None,
) -> SignedRecord:
    start_block = _require_int(start_block, name="start_block", minimum=0)
    end_block = _require_int(end_block, name="end_block", minimum=1)
    if end_block <= start_block:
        raise ProtocolSecurityError("intent lease end_block must be greater than start_block")
    normalized_claims = _validate_intent_resource_claims(resource_claims)
    if normalized_claims["block_count"] != end_block - start_block:
        raise ProtocolSecurityError("intent lease block_count does not match its block range")
    payload = {
        "peer_id": identity.peer_id.to_base58(),
        "manifest_digest": _require_digest(manifest_digest, name="manifest_digest"),
        "start_block": start_block,
        "end_block": end_block,
        "resource_claims": normalized_claims,
        "issued_at_ms": int(issued_at * 1000),
        "expires_at_ms": int(expires_at * 1000),
        "sequence": _require_int(sequence, name="sequence", minimum=0),
        "nonce": secrets.token_hex(16) if nonce is None else _require_text(nonce, name="nonce"),
    }
    _validate_lifetime(payload, now=issued_at)
    return SignedRecord.create("intent_lease", payload, identity)


def verify_intent_lease(
    source: Mapping[str, Any],
    *,
    expected_manifest_digest: Optional[str] = None,
    now: Optional[float] = None,
    revocations: Optional[RevocationStore] = None,
    replay_guard: Optional[ReplayGuard] = None,
) -> SignedRecord:
    record = SignedRecord.from_dict(source)
    record.verify(expected_kind="intent_lease")
    fields = (
        "peer_id",
        "manifest_digest",
        "start_block",
        "end_block",
        "resource_claims",
        "issued_at_ms",
        "expires_at_ms",
        "sequence",
        "nonce",
    )
    _strict_fields(record.payload, fields, name="intent lease payload")
    if record.payload["peer_id"] != record.peer_id.to_base58():
        raise ProtocolSecurityError("intent lease public key does not derive its claimed PeerID")
    digest = _require_digest(record.payload["manifest_digest"], name="manifest_digest")
    if expected_manifest_digest is not None and digest != expected_manifest_digest:
        raise ProtocolSecurityError("intent lease is bound to a different manifest")
    start = _require_int(record.payload["start_block"], name="start_block", minimum=0)
    end = _require_int(record.payload["end_block"], name="end_block", minimum=1)
    if end <= start:
        raise ProtocolSecurityError("intent lease end_block must be greater than start_block")
    resource_claims = _validate_intent_resource_claims(record.payload["resource_claims"])
    if resource_claims["block_count"] != end - start:
        raise ProtocolSecurityError("intent lease block_count does not match its block range")
    _require_text(record.payload["nonce"], name="nonce")
    _validate_lifetime(record.payload, now=now)
    if revocations is not None:
        revocations.require_active(record.key_id)
    if replay_guard is not None:
        replay_guard.check(record)
    return record


ROUTE_DEMAND_SCHEMA_VERSION = 1
ROUTE_DEMAND_MAX_TTL_SECONDS = 90
_ROUTE_DEMAND_FIELDS = (
    "schema_version",
    "manifest_digest",
    "window_seconds",
    "attempts_bucket",
    "successes_bucket",
    "useful_tokens_per_second_milli",
    "reliability_milli",
    "age_seconds_bucket",
)
_ROUTE_DEMAND_COUNT_BUCKETS = {0, 1, 2, 4, 8, 16, 32, 64}
_ROUTE_DEMAND_THROUGHPUT_BUCKETS = {0, 250, 500, 1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 64_000}


def _validate_route_demand_observation(
    value: Mapping[str, Any], *, expected_manifest_digest: Optional[str] = None
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolSecurityError("route demand observation must be an object")
    _strict_fields(value, _ROUTE_DEMAND_FIELDS, name="route demand observation")
    schema_version = _require_int(value["schema_version"], name="route demand schema_version", minimum=1)
    if schema_version != ROUTE_DEMAND_SCHEMA_VERSION:
        raise ProtocolSecurityError(f"unsupported route demand schema version {schema_version}")
    manifest_digest = _require_text(value["manifest_digest"], name="route demand manifest_digest")
    if not manifest_digest.startswith("sha256:"):
        raise ProtocolSecurityError("route demand manifest_digest must use the sha256: prefix")
    digest = _require_digest(manifest_digest.removeprefix("sha256:"), name="route demand manifest_digest")
    if expected_manifest_digest is not None and digest != expected_manifest_digest:
        raise ProtocolSecurityError("route demand is bound to a different manifest")
    window_seconds = _require_int(value["window_seconds"], name="route demand window_seconds", minimum=60)
    if window_seconds != 5 * 60:
        raise ProtocolSecurityError("route demand must use a closed five-minute window")
    attempts = _require_int(value["attempts_bucket"], name="route demand attempts_bucket", minimum=4)
    successes = _require_int(value["successes_bucket"], name="route demand successes_bucket", minimum=0)
    if (
        attempts not in _ROUTE_DEMAND_COUNT_BUCKETS
        or successes not in _ROUTE_DEMAND_COUNT_BUCKETS
        or successes > attempts
    ):
        raise ProtocolSecurityError("route demand count buckets are invalid")
    throughput = _require_int(value["useful_tokens_per_second_milli"], name="route demand throughput bucket", minimum=0)
    if throughput not in _ROUTE_DEMAND_THROUGHPUT_BUCKETS:
        raise ProtocolSecurityError("route demand throughput bucket is invalid")
    reliability = _require_int(value["reliability_milli"], name="route demand reliability bucket", minimum=0)
    if reliability > 1000 or reliability % 100:
        raise ProtocolSecurityError("route demand reliability must use 10-percent buckets")
    age = _require_int(value["age_seconds_bucket"], name="route demand age bucket", minimum=0)
    if age > window_seconds or age % 15:
        raise ProtocolSecurityError("route demand age must use bounded 15-second buckets")
    return {
        "schema_version": schema_version,
        "manifest_digest": f"sha256:{digest}",
        "window_seconds": window_seconds,
        "attempts_bucket": attempts,
        "successes_bucket": successes,
        "useful_tokens_per_second_milli": throughput,
        "reliability_milli": reliability,
        "age_seconds_bucket": age,
    }


def _validate_route_demand_lifetime(payload: Mapping[str, Any], *, now: Optional[float]) -> Tuple[int, int]:
    issued_at_ms, expires_at_ms = _validate_lifetime(payload, now=now)
    if expires_at_ms - issued_at_ms > ROUTE_DEMAND_MAX_TTL_SECONDS * 1000:
        raise ProtocolSecurityError("route demand lifetime exceeds 90 seconds")
    return issued_at_ms, expires_at_ms


def create_route_demand(
    identity: NodeIdentity,
    *,
    manifest_digest: str,
    observation: Mapping[str, Any],
    issued_at: float,
    expires_at: float,
    sequence: int,
) -> SignedRecord:
    digest = _require_digest(manifest_digest, name="manifest_digest")
    payload = {
        "manifest_digest": digest,
        "observation": _validate_route_demand_observation(observation, expected_manifest_digest=digest),
        "issued_at_ms": int(issued_at * 1000),
        "expires_at_ms": int(expires_at * 1000),
        "sequence": _require_int(sequence, name="sequence", minimum=0),
    }
    _validate_route_demand_lifetime(payload, now=issued_at)
    return SignedRecord.create("route_demand", payload, identity)


def verify_route_demand(
    source: Mapping[str, Any],
    *,
    expected_manifest_digest: Optional[str] = None,
    now: Optional[float] = None,
    revocations: Optional[RevocationStore] = None,
    replay_guard: Optional[ReplayGuard] = None,
) -> SignedRecord:
    record = SignedRecord.from_dict(source)
    record.verify(expected_kind="route_demand")
    _strict_fields(
        record.payload,
        ("manifest_digest", "observation", "issued_at_ms", "expires_at_ms", "sequence"),
        name="route demand payload",
    )
    digest = _require_digest(record.payload["manifest_digest"], name="manifest_digest")
    if expected_manifest_digest is not None and digest != expected_manifest_digest:
        raise ProtocolSecurityError("route demand is bound to a different manifest")
    _validate_route_demand_observation(record.payload["observation"], expected_manifest_digest=digest)
    _validate_route_demand_lifetime(record.payload, now=now)
    _require_int(record.payload["sequence"], name="sequence", minimum=0)
    if revocations is not None:
        revocations.require_active(record.key_id)
    if replay_guard is not None:
        replay_guard.check(record)
    return record


def create_rotation_record(
    old_identity: NodeIdentity, new_identity: NodeIdentity, *, issued_at: Optional[float] = None, sequence: int = 0
) -> Dict[str, Any]:
    issued_at_ms = int((time.time() if issued_at is None else issued_at) * 1000)
    payload = {
        "old_key_id": old_identity.key_id,
        "old_peer_id": old_identity.peer_id.to_base58(),
        "new_key_id": new_identity.key_id,
        "new_peer_id": new_identity.peer_id.to_base58(),
        "issued_at_ms": issued_at_ms,
        "sequence": _require_int(sequence, name="sequence", minimum=0),
    }
    old_proof = SignedRecord.create("identity_rotation_old", payload, old_identity)
    new_proof = SignedRecord.create("identity_rotation_new", payload, new_identity)
    return {
        "schema_version": SIGNED_RECORD_SCHEMA_VERSION,
        "kind": "identity_rotation",
        "payload": payload,
        "old_proof": old_proof.to_dict(),
        "new_proof": new_proof.to_dict(),
    }


def verify_rotation_record(source: Mapping[str, Any]) -> Tuple[str, str]:
    fields = ("schema_version", "kind", "payload", "old_proof", "new_proof")
    _strict_fields(source, fields, name="identity rotation")
    if source["schema_version"] != SIGNED_RECORD_SCHEMA_VERSION or source["kind"] != "identity_rotation":
        raise ProtocolSecurityError("unsupported identity rotation record")
    if not isinstance(source["payload"], Mapping):
        raise ProtocolSecurityError("identity rotation payload must be an object")
    payload = _normalize_json(source["payload"], path="identity rotation payload")
    payload_fields = ("old_key_id", "old_peer_id", "new_key_id", "new_peer_id", "issued_at_ms", "sequence")
    _strict_fields(payload, payload_fields, name="identity rotation payload")
    old_proof, new_proof = SignedRecord.from_dict(source["old_proof"]), SignedRecord.from_dict(source["new_proof"])
    old_proof.verify(expected_kind="identity_rotation_old")
    new_proof.verify(expected_kind="identity_rotation_new")
    if old_proof.payload != payload or new_proof.payload != payload:
        raise ProtocolSecurityError("identity rotation proofs do not cover the same payload")
    _require_key_id(payload["old_key_id"], name="old_key_id")
    _require_key_id(payload["new_key_id"], name="new_key_id")
    if old_proof.key_id != payload["old_key_id"] or old_proof.peer_id.to_base58() != payload["old_peer_id"]:
        raise ProtocolSecurityError("identity rotation old proof does not match the old identity")
    if new_proof.key_id != payload["new_key_id"] or new_proof.peer_id.to_base58() != payload["new_peer_id"]:
        raise ProtocolSecurityError("identity rotation new proof does not match the new identity")
    if old_proof.key_id == new_proof.key_id:
        raise ProtocolSecurityError("identity rotation must change the key")
    _require_int(payload["issued_at_ms"], name="issued_at_ms", minimum=0)
    _require_int(payload["sequence"], name="sequence", minimum=0)
    return old_proof.key_id, new_proof.key_id


def create_revocation_record(
    authority: NodeIdentity,
    *,
    revoked_key_id: Optional[str] = None,
    revoked_peer_id: Optional[str] = None,
    reason: str = "",
    issued_at: Optional[float] = None,
    sequence: int = 0,
) -> Dict[str, Any]:
    revoked_key_id = (
        authority.key_id if revoked_key_id is None else _require_key_id(revoked_key_id, name="revoked_key_id")
    )
    revoked_peer_id = (
        authority.peer_id.to_base58()
        if revoked_peer_id is None
        else _require_text(revoked_peer_id, name="revoked_peer_id")
    )
    payload = {
        "authority_key_id": authority.key_id,
        "revoked_key_id": revoked_key_id,
        "revoked_peer_id": revoked_peer_id,
        "issued_at_ms": int((time.time() if issued_at is None else issued_at) * 1000),
        "sequence": _require_int(sequence, name="sequence", minimum=0),
        "reason": _require_text(reason, name="reason", allow_empty=True),
    }
    return SignedRecord.create("identity_revocation", payload, authority).to_dict()
