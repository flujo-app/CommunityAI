"""Strict signed model catalogs and elastic capacity-ladder selection.

Catalogs approve exact :class:`~drift.model_manifest.ModelManifest` digests. They
never replace manifest identity: the catalog only groups immutable manifests into
ordered capacity rungs and publishes the safety gates for selecting the highest
currently usable rung.

The trust root is deliberately separate from the catalog document. A catalog
cannot authorize its own signing key, and an installation can replace or add roots
without changing the inference protocol.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from cryptography import exceptions
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

CATALOG_SCHEMA_VERSION = 1
CATALOG_ROOT_SCHEMA_VERSION = 1
CATALOG_STATE_SCHEMA_VERSION = 1
CATALOG_SIGNATURE_ALGORITHM = "ed25519"
CATALOG_SIGNATURE_DOMAIN = b"communityai-model-catalog-v1\x00"
MAX_CATALOG_LIFETIME_SECONDS = 180 * 24 * 60 * 60
MAX_CATALOG_CLOCK_SKEW_SECONDS = 5 * 60

_RUNG_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class ModelCatalogError(ValueError):
    """A catalog, trust root, signature, or promotion observation failed closed."""


def _strict_fields(
    value: Mapping[str, Any], name: str, *, required: Iterable[str], optional: Iterable[str] = ()
) -> None:
    expected = set(required) | set(optional)
    actual = set(value)
    missing = set(required) - actual
    unknown = actual - expected
    if missing:
        raise ModelCatalogError(f"{name} is missing required fields: {sorted(missing)}")
    if unknown:
        raise ModelCatalogError(f"{name} contains unknown fields: {sorted(unknown)}")


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelCatalogError(f"{name} must be a JSON object")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelCatalogError(f"{name} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ModelCatalogError(f"{name} must be NFC-normalized")
    return value


def _require_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelCatalogError(f"{name} must be an integer >= {minimum}")
    return value


def _require_digest_id(value: Any, name: str) -> str:
    value = _require_text(value, name)
    if not value.startswith("sha256:"):
        raise ModelCatalogError(f"{name} must be a sha256: digest identifier")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or digest.lower() != digest or any(char not in "0123456789abcdef" for char in digest):
        raise ModelCatalogError(f"{name} must be a sha256: digest identifier")
    return value


def _require_b64(value: Any, name: str) -> bytes:
    value = _require_text(value, name)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ModelCatalogError(f"{name} must be canonical base64") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ModelCatalogError(f"{name} must be canonical base64")
    return decoded


def _normalize_json(value: Any, *, path: str = "value") -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and unicodedata.normalize("NFC", value) != value:
            raise ModelCatalogError(f"{path} must contain only NFC-normalized strings")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelCatalogError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, path=f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ModelCatalogError(f"{path} contains an empty or non-string object key")
            result[_require_text(key, f"{path} key")] = _normalize_json(item, path=f"{path}.{key}")
        return result
    raise ModelCatalogError(f"{path} contains unsupported value {type(value).__name__}")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        _normalize_json(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _load_strict_json(source: str, *, name: str) -> Mapping[str, Any]:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ModelCatalogError(f"{name} contains duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise ModelCatalogError(f"{name} contains non-finite number {value}")

    try:
        value = json.loads(source, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
    except json.JSONDecodeError as exc:
        raise ModelCatalogError(f"Invalid {name} JSON: {exc}") from exc
    return _require_mapping(value, name)


def _read_text(path: Path | str, *, name: str) -> str:
    resolved = Path(path).expanduser().resolve()
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ModelCatalogError(f"Could not read {name} {resolved}: {exc}") from exc


@dataclass(frozen=True)
class CatalogTrustedKey:
    key_id: str
    algorithm: str
    public_key: str

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], *, name: str = "catalog trusted key") -> "CatalogTrustedKey":
        source = _require_mapping(source, name)
        _strict_fields(source, name, required=("key_id", "algorithm", "public_key"))
        if source["algorithm"] != CATALOG_SIGNATURE_ALGORITHM:
            raise ModelCatalogError(f"{name}.algorithm must be {CATALOG_SIGNATURE_ALGORITHM!r}")
        public_key_der = _require_b64(source["public_key"], f"{name}.public_key")
        try:
            key = serialization.load_der_public_key(public_key_der)
        except (TypeError, ValueError) as exc:
            raise ModelCatalogError(f"{name}.public_key is not a valid DER public key") from exc
        if not isinstance(key, ed25519.Ed25519PublicKey):
            raise ModelCatalogError(f"{name}.public_key must be an Ed25519 public key")
        expected_key_id = f"sha256:{hashlib.sha256(public_key_der).hexdigest()}"
        key_id = _require_digest_id(source["key_id"], f"{name}.key_id")
        if key_id != expected_key_id:
            raise ModelCatalogError(f"{name}.key_id does not match its public key")
        return cls(key_id=key_id, algorithm=source["algorithm"], public_key=source["public_key"])

    @property
    def public_key_object(self) -> ed25519.Ed25519PublicKey:
        key = serialization.load_der_public_key(_require_b64(self.public_key, "catalog trusted key public_key"))
        assert isinstance(key, ed25519.Ed25519PublicKey)
        return key

    def to_dict(self) -> Dict[str, Any]:
        return {"key_id": self.key_id, "algorithm": self.algorithm, "public_key": self.public_key}


@dataclass(frozen=True)
class CatalogSigningKey:
    """An offline catalog key, intentionally distinct from a worker/libp2p identity."""

    _private_key: ed25519.Ed25519PrivateKey = field(repr=False)

    @classmethod
    def generate(cls) -> "CatalogSigningKey":
        return cls(ed25519.Ed25519PrivateKey.generate())

    @classmethod
    def load(cls, path: Path | str) -> "CatalogSigningKey":
        resolved = Path(path).expanduser().resolve()
        try:
            private_key = serialization.load_pem_private_key(resolved.read_bytes(), password=None)
        except (OSError, TypeError, ValueError) as exc:
            raise ModelCatalogError(f"Could not load catalog signing key {resolved}: {exc}") from exc
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise ModelCatalogError("Catalog signing keys must be Ed25519 private keys")
        return cls(private_key)

    @property
    def public_key_der(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    @property
    def key_id(self) -> str:
        return f"sha256:{hashlib.sha256(self.public_key_der).hexdigest()}"

    @property
    def trusted_key(self) -> CatalogTrustedKey:
        return CatalogTrustedKey.from_dict(
            {
                "key_id": self.key_id,
                "algorithm": CATALOG_SIGNATURE_ALGORITHM,
                "public_key": base64.b64encode(self.public_key_der).decode("ascii"),
            }
        )

    def sign(self, data: bytes) -> bytes:
        return self._private_key.sign(data)

    def save(self, path: Path | str, *, overwrite: bool = False) -> None:
        resolved = Path(path).expanduser().resolve()
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ModelCatalogError(f"Could not create catalog key directory {resolved.parent}: {exc}") from exc
        if overwrite:
            temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            try:
                self._save_new(temporary)
                os.replace(temporary, resolved)
            except OSError as exc:
                raise ModelCatalogError(f"Could not replace catalog signing key {resolved}: {exc}") from exc
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return
        self._save_new(resolved)

    def _save_new(self, resolved: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(resolved, flags, 0o600)
        except FileExistsError as exc:
            raise ModelCatalogError(f"Catalog signing key already exists: {resolved}") from exc
        except OSError as exc:
            raise ModelCatalogError(f"Could not create catalog signing key {resolved}: {exc}") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(
                    self._private_key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
        except BaseException as exc:
            try:
                resolved.unlink()
            except OSError:
                pass
            if isinstance(exc, OSError):
                raise ModelCatalogError(f"Could not write catalog signing key {resolved}: {exc}") from exc
            raise


@dataclass(frozen=True)
class CatalogTrustRoot:
    schema_version: int
    catalog_id: str
    threshold: int
    keys: Tuple[CatalogTrustedKey, ...]

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "CatalogTrustRoot":
        source = _require_mapping(source, "catalog trust root")
        _strict_fields(source, "catalog trust root", required=("schema_version", "catalog_id", "threshold", "keys"))
        schema_version = _require_int(source["schema_version"], "catalog trust root schema_version", minimum=1)
        if schema_version != CATALOG_ROOT_SCHEMA_VERSION:
            raise ModelCatalogError(f"Unsupported catalog trust-root schema version {schema_version}")
        catalog_id = _require_text(source["catalog_id"], "catalog trust root catalog_id")
        threshold = _require_int(source["threshold"], "catalog trust root threshold", minimum=1)
        if not isinstance(source["keys"], list) or not source["keys"]:
            raise ModelCatalogError("catalog trust root keys must be a non-empty array")
        keys = tuple(
            CatalogTrustedKey.from_dict(item, name=f"catalog trust root keys[{index}]")
            for index, item in enumerate(source["keys"])
        )
        key_ids = [key.key_id for key in keys]
        if len(set(key_ids)) != len(key_ids):
            raise ModelCatalogError("catalog trust root key ids must be unique")
        if threshold > len(keys):
            raise ModelCatalogError("catalog trust root threshold cannot exceed its number of keys")
        return cls(schema_version=schema_version, catalog_id=catalog_id, threshold=threshold, keys=keys)

    @classmethod
    def from_json(cls, source: str) -> "CatalogTrustRoot":
        return cls.from_dict(_load_strict_json(source, name="catalog trust root"))

    @classmethod
    def load(cls, path: Path | str) -> "CatalogTrustRoot":
        return cls.from_json(_read_text(path, name="catalog trust root"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_id": self.catalog_id,
            "threshold": self.threshold,
            "keys": [key.to_dict() for key in self.keys],
        }


def _require_https_url(value: Any, name: str) -> str:
    value = _require_text(value, name)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise ModelCatalogError(f"{name} must be an absolute HTTPS URL without credentials or a fragment")
    return value


@dataclass(frozen=True)
class CatalogRung:
    rung_id: str
    order: int
    minimum_replicas: int
    minimum_independent_routes: int
    minimum_surviving_replicas: int
    minimum_soak_seconds: int
    maximum_observation_age_seconds: int
    maximum_p95_first_token_ms: int
    minimum_tokens_per_minute: int

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], *, index: int) -> "CatalogRung":
        name = f"catalog rungs[{index}]"
        source = _require_mapping(source, name)
        fields = (
            "id",
            "order",
            "minimum_replicas",
            "minimum_independent_routes",
            "minimum_surviving_replicas",
            "minimum_soak_seconds",
            "maximum_observation_age_seconds",
            "maximum_p95_first_token_ms",
            "minimum_tokens_per_minute",
        )
        _strict_fields(source, name, required=fields)
        rung_id = _require_text(source["id"], f"{name}.id")
        if _RUNG_ID_RE.fullmatch(rung_id) is None:
            raise ModelCatalogError(f"{name}.id must match {_RUNG_ID_RE.pattern}")
        minimum_replicas = _require_int(source["minimum_replicas"], f"{name}.minimum_replicas", minimum=1)
        minimum_surviving = _require_int(
            source["minimum_surviving_replicas"], f"{name}.minimum_surviving_replicas", minimum=1
        )
        if minimum_surviving > minimum_replicas:
            raise ModelCatalogError(f"{name}.minimum_surviving_replicas cannot exceed minimum_replicas")
        return cls(
            rung_id=rung_id,
            order=_require_int(source["order"], f"{name}.order", minimum=1),
            minimum_replicas=minimum_replicas,
            minimum_independent_routes=_require_int(
                source["minimum_independent_routes"], f"{name}.minimum_independent_routes", minimum=1
            ),
            minimum_surviving_replicas=minimum_surviving,
            minimum_soak_seconds=_require_int(
                source["minimum_soak_seconds"], f"{name}.minimum_soak_seconds", minimum=0
            ),
            maximum_observation_age_seconds=_require_int(
                source["maximum_observation_age_seconds"], f"{name}.maximum_observation_age_seconds", minimum=1
            ),
            maximum_p95_first_token_ms=_require_int(
                source["maximum_p95_first_token_ms"], f"{name}.maximum_p95_first_token_ms", minimum=1
            ),
            minimum_tokens_per_minute=_require_int(
                source["minimum_tokens_per_minute"], f"{name}.minimum_tokens_per_minute", minimum=1
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.rung_id,
            "order": self.order,
            "minimum_replicas": self.minimum_replicas,
            "minimum_independent_routes": self.minimum_independent_routes,
            "minimum_surviving_replicas": self.minimum_surviving_replicas,
            "minimum_soak_seconds": self.minimum_soak_seconds,
            "maximum_observation_age_seconds": self.maximum_observation_age_seconds,
            "maximum_p95_first_token_ms": self.maximum_p95_first_token_ms,
            "minimum_tokens_per_minute": self.minimum_tokens_per_minute,
        }


@dataclass(frozen=True)
class CatalogModel:
    manifest_digest: str
    manifest_urls: Tuple[str, ...]
    rung_id: str
    role: str
    total_parameters: int
    active_parameters: int
    weight_bytes: int

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], *, index: int) -> "CatalogModel":
        name = f"catalog models[{index}]"
        source = _require_mapping(source, name)
        fields = (
            "manifest_digest",
            "manifest_urls",
            "rung",
            "role",
            "total_parameters",
            "active_parameters",
            "weight_bytes",
        )
        _strict_fields(source, name, required=fields)
        urls_value = source["manifest_urls"]
        if not isinstance(urls_value, list) or not urls_value:
            raise ModelCatalogError(f"{name}.manifest_urls must be a non-empty array")
        urls = tuple(_require_https_url(value, f"{name}.manifest_urls[]") for value in urls_value)
        if len(set(urls)) != len(urls):
            raise ModelCatalogError(f"{name}.manifest_urls must not contain duplicates")
        role = _require_text(source["role"], f"{name}.role")
        if role not in {"primary", "standby"}:
            raise ModelCatalogError(f"{name}.role must be 'primary' or 'standby'")
        total_parameters = _require_int(source["total_parameters"], f"{name}.total_parameters", minimum=1)
        active_parameters = _require_int(source["active_parameters"], f"{name}.active_parameters", minimum=1)
        if active_parameters > total_parameters:
            raise ModelCatalogError(f"{name}.active_parameters cannot exceed total_parameters")
        return cls(
            manifest_digest=_require_digest_id(source["manifest_digest"], f"{name}.manifest_digest"),
            manifest_urls=urls,
            rung_id=_require_text(source["rung"], f"{name}.rung"),
            role=role,
            total_parameters=total_parameters,
            active_parameters=active_parameters,
            weight_bytes=_require_int(source["weight_bytes"], f"{name}.weight_bytes", minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "manifest_urls": list(self.manifest_urls),
            "rung": self.rung_id,
            "role": self.role,
            "total_parameters": self.total_parameters,
            "active_parameters": self.active_parameters,
            "weight_bytes": self.weight_bytes,
        }


@dataclass(frozen=True)
class ModelCatalog:
    catalog_id: str
    sequence: int
    issued_at_ms: int
    expires_at_ms: int
    rungs: Tuple[CatalogRung, ...]
    models: Tuple[CatalogModel, ...]

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "ModelCatalog":
        source = _require_mapping(source, "model catalog")
        fields = ("catalog_id", "sequence", "issued_at_ms", "expires_at_ms", "rungs", "models")
        _strict_fields(source, "model catalog", required=fields)
        rungs_value, models_value = source["rungs"], source["models"]
        if not isinstance(rungs_value, list) or not rungs_value:
            raise ModelCatalogError("model catalog rungs must be a non-empty array")
        if not isinstance(models_value, list) or not models_value:
            raise ModelCatalogError("model catalog models must be a non-empty array")
        rungs = tuple(CatalogRung.from_dict(item, index=index) for index, item in enumerate(rungs_value))
        models = tuple(CatalogModel.from_dict(item, index=index) for index, item in enumerate(models_value))
        rung_ids = [rung.rung_id for rung in rungs]
        orders = [rung.order for rung in rungs]
        if len(set(rung_ids)) != len(rung_ids):
            raise ModelCatalogError("model catalog rung ids must be unique")
        if len(set(orders)) != len(orders):
            raise ModelCatalogError("model catalog rung orders must be unique")
        if orders != sorted(orders):
            raise ModelCatalogError("model catalog rungs must be sorted by ascending order")
        digests = [model.manifest_digest for model in models]
        if len(set(digests)) != len(digests):
            raise ModelCatalogError("model catalog manifest digests must be unique")
        known_rungs = set(rung_ids)
        if any(model.rung_id not in known_rungs for model in models):
            raise ModelCatalogError("every catalog model must reference a declared rung")
        for rung_id in rung_ids:
            rung_models = [model for model in models if model.rung_id == rung_id]
            if len(rung_models) < 2:
                raise ModelCatalogError(f"catalog rung {rung_id!r} must approve at least two model options")
            primary_count = sum(model.role == "primary" for model in rung_models)
            if primary_count != 1:
                raise ModelCatalogError(f"catalog rung {rung_id!r} must declare exactly one primary model")
        issued_at_ms = _require_int(source["issued_at_ms"], "model catalog issued_at_ms", minimum=0)
        expires_at_ms = _require_int(source["expires_at_ms"], "model catalog expires_at_ms", minimum=1)
        if expires_at_ms <= issued_at_ms:
            raise ModelCatalogError("model catalog must expire after it was issued")
        if expires_at_ms - issued_at_ms > MAX_CATALOG_LIFETIME_SECONDS * 1000:
            raise ModelCatalogError("model catalog lifetime exceeds the v1 maximum")
        return cls(
            catalog_id=_require_text(source["catalog_id"], "model catalog catalog_id"),
            sequence=_require_int(source["sequence"], "model catalog sequence", minimum=1),
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            rungs=rungs,
            models=models,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "sequence": self.sequence,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "rungs": [rung.to_dict() for rung in self.rungs],
            "models": [model.to_dict() for model in self.models],
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.canonical_json.encode('utf-8')).hexdigest()}"

    def validate_time(self, *, now: Optional[float] = None) -> None:
        current_ms = int((time.time() if now is None else now) * 1000)
        if self.issued_at_ms > current_ms + MAX_CATALOG_CLOCK_SKEW_SECONDS * 1000:
            raise ModelCatalogError("model catalog was issued too far in the future")
        if self.expires_at_ms <= current_ms:
            raise ModelCatalogError("model catalog has expired")


@dataclass(frozen=True)
class CatalogSignature:
    key_id: str
    algorithm: str
    signature: str

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], *, index: int) -> "CatalogSignature":
        name = f"catalog signatures[{index}]"
        source = _require_mapping(source, name)
        _strict_fields(source, name, required=("key_id", "algorithm", "signature"))
        key_id = _require_digest_id(source["key_id"], f"{name}.key_id")
        if source["algorithm"] != CATALOG_SIGNATURE_ALGORITHM:
            raise ModelCatalogError(f"{name}.algorithm must be {CATALOG_SIGNATURE_ALGORITHM!r}")
        _require_b64(source["signature"], f"{name}.signature")
        return cls(key_id=key_id, algorithm=source["algorithm"], signature=source["signature"])

    def to_dict(self) -> Dict[str, Any]:
        return {"key_id": self.key_id, "algorithm": self.algorithm, "signature": self.signature}


@dataclass(frozen=True)
class SignedModelCatalog:
    schema_version: int
    signed: ModelCatalog
    signatures: Tuple[CatalogSignature, ...]

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "SignedModelCatalog":
        source = _require_mapping(source, "signed model catalog")
        _strict_fields(source, "signed model catalog", required=("schema_version", "signed", "signatures"))
        schema_version = _require_int(source["schema_version"], "signed model catalog schema_version", minimum=1)
        if schema_version != CATALOG_SCHEMA_VERSION:
            raise ModelCatalogError(f"Unsupported signed catalog schema version {schema_version}")
        if not isinstance(source["signatures"], list):
            raise ModelCatalogError("signed model catalog signatures must be an array")
        signatures = tuple(
            CatalogSignature.from_dict(item, index=index) for index, item in enumerate(source["signatures"])
        )
        key_ids = [signature.key_id for signature in signatures]
        if len(set(key_ids)) != len(key_ids):
            raise ModelCatalogError("signed model catalog must not contain duplicate key signatures")
        return cls(
            schema_version=schema_version,
            signed=ModelCatalog.from_dict(source["signed"]),
            signatures=signatures,
        )

    @classmethod
    def from_json(cls, source: str) -> "SignedModelCatalog":
        return cls.from_dict(_load_strict_json(source, name="signed model catalog"))

    @classmethod
    def load(cls, path: Path | str) -> "SignedModelCatalog":
        return cls.from_json(_read_text(path, name="signed model catalog"))

    @property
    def signing_bytes(self) -> bytes:
        document = {"schema_version": self.schema_version, "signed": self.signed.to_dict()}
        return CATALOG_SIGNATURE_DOMAIN + _canonical_json(document).encode("utf-8")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signed": self.signed.to_dict(),
            "signatures": [signature.to_dict() for signature in self.signatures],
        }

    def add_signature(self, signing_key: CatalogSigningKey) -> "SignedModelCatalog":
        encoded = base64.b64encode(signing_key.sign(self.signing_bytes)).decode("ascii")
        signature = CatalogSignature(
            key_id=signing_key.key_id,
            algorithm=CATALOG_SIGNATURE_ALGORITHM,
            signature=encoded,
        )
        retained = tuple(item for item in self.signatures if item.key_id != signature.key_id)
        signatures = tuple(sorted((*retained, signature), key=lambda item: item.key_id))
        return SignedModelCatalog(self.schema_version, self.signed, signatures)

    def verify(
        self,
        trust_root: CatalogTrustRoot,
        *,
        now: Optional[float] = None,
        rollback_guard: Optional["CatalogRollbackGuard"] = None,
    ) -> ModelCatalog:
        if self.signed.catalog_id != trust_root.catalog_id:
            raise ModelCatalogError("model catalog id does not match its trusted root")
        self.signed.validate_time(now=now)
        trusted = {key.key_id: key for key in trust_root.keys}
        valid = set()
        for signature in self.signatures:
            key = trusted.get(signature.key_id)
            if key is None:
                raise ModelCatalogError(f"model catalog contains untrusted signature {signature.key_id}")
            try:
                key.public_key_object.verify(_require_b64(signature.signature, "catalog signature"), self.signing_bytes)
            except exceptions.InvalidSignature as exc:
                raise ModelCatalogError(f"model catalog signature from {signature.key_id} is invalid") from exc
            valid.add(signature.key_id)
        if len(valid) < trust_root.threshold:
            raise ModelCatalogError(
                f"model catalog has {len(valid)} valid trusted signature(s); {trust_root.threshold} required"
            )
        if rollback_guard is not None:
            rollback_guard.check(self.signed)
        return self.signed


@dataclass
class CatalogRollbackGuard:
    """Persistable per-catalog sequence state for rollback and equivocation rejection."""

    latest: Dict[str, Tuple[int, str]] = field(default_factory=dict)

    def check(self, catalog: ModelCatalog) -> None:
        current = self.latest.get(catalog.catalog_id)
        proposed = (catalog.sequence, catalog.digest)
        if current is not None:
            if catalog.sequence < current[0]:
                raise ModelCatalogError("model catalog sequence is older than the last accepted catalog")
            if catalog.sequence == current[0] and catalog.digest != current[1]:
                raise ModelCatalogError("model catalog equivocated at an already accepted sequence")
        self.latest[catalog.catalog_id] = proposed

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "CatalogRollbackGuard":
        source = _require_mapping(source, "catalog rollback state")
        _strict_fields(source, "catalog rollback state", required=("schema_version", "catalogs"))
        version = _require_int(source["schema_version"], "catalog rollback state schema_version", minimum=1)
        if version != CATALOG_STATE_SCHEMA_VERSION:
            raise ModelCatalogError(f"Unsupported catalog rollback-state schema version {version}")
        catalogs = _require_mapping(source["catalogs"], "catalog rollback state catalogs")
        latest = {}
        for catalog_id, item in catalogs.items():
            catalog_id = _require_text(catalog_id, "catalog rollback state catalog id")
            item = _require_mapping(item, f"catalog rollback state {catalog_id!r}")
            _strict_fields(item, f"catalog rollback state {catalog_id!r}", required=("sequence", "digest"))
            latest[catalog_id] = (
                _require_int(item["sequence"], f"catalog rollback state {catalog_id!r} sequence", minimum=1),
                _require_digest_id(item["digest"], f"catalog rollback state {catalog_id!r} digest"),
            )
        return cls(latest=latest)

    @classmethod
    def load(cls, path: Path | str) -> "CatalogRollbackGuard":
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            return cls()
        return cls.from_dict(
            _load_strict_json(_read_text(resolved, name="catalog rollback state"), name="catalog rollback state")
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": CATALOG_STATE_SCHEMA_VERSION,
            "catalogs": {
                catalog_id: {"sequence": sequence, "digest": digest}
                for catalog_id, (sequence, digest) in sorted(self.latest.items())
            },
        }

    def save(self, path: Path | str) -> None:
        resolved = Path(path).expanduser().resolve()
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ModelCatalogError(f"Could not create rollback-state directory {resolved.parent}: {exc}") from exc
        temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        rendered = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            temporary.write_text(rendered, encoding="utf-8", newline="\n")
            os.replace(temporary, resolved)
        except OSError as exc:
            raise ModelCatalogError(f"Could not write catalog rollback state {resolved}: {exc}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class CapacityObservation:
    """Measured, local evidence for one exact manifested swarm."""

    manifest_digest: str
    observed_at_ms: int
    stable_since_ms: int
    bottleneck_replicas: float
    independent_routes: int
    replicas_after_largest_peer_loss: float
    p95_first_token_ms: int
    tokens_per_minute: int

    def __post_init__(self) -> None:
        _require_digest_id(self.manifest_digest, "capacity observation manifest_digest")
        _require_int(self.observed_at_ms, "capacity observation observed_at_ms", minimum=0)
        _require_int(self.stable_since_ms, "capacity observation stable_since_ms", minimum=0)
        if self.stable_since_ms > self.observed_at_ms:
            raise ModelCatalogError("capacity observation stable_since_ms cannot follow observed_at_ms")
        for value, name in (
            (self.bottleneck_replicas, "bottleneck_replicas"),
            (self.replicas_after_largest_peer_loss, "replicas_after_largest_peer_loss"),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ModelCatalogError(f"capacity observation {name} must be a finite number >= 0")
        _require_int(self.independent_routes, "capacity observation independent_routes", minimum=0)
        _require_int(self.p95_first_token_ms, "capacity observation p95_first_token_ms", minimum=0)
        _require_int(self.tokens_per_minute, "capacity observation tokens_per_minute", minimum=0)


@dataclass(frozen=True)
class ModelEligibility:
    model: CatalogModel
    eligible: bool
    reasons: Tuple[str, ...]


def evaluate_model_eligibility(
    model: CatalogModel,
    rung: CatalogRung,
    observation: Optional[CapacityObservation],
    *,
    now_ms: int,
) -> ModelEligibility:
    reasons = []
    if observation is None:
        reasons.append("no capacity observation")
    else:
        if observation.manifest_digest != model.manifest_digest:
            reasons.append("observation manifest mismatch")
        if observation.observed_at_ms > now_ms:
            reasons.append("observation is from the future")
        elif now_ms - observation.observed_at_ms > rung.maximum_observation_age_seconds * 1000:
            reasons.append("capacity observation is stale")
        if observation.bottleneck_replicas < rung.minimum_replicas:
            reasons.append("insufficient bottleneck replica coverage")
        if observation.independent_routes < rung.minimum_independent_routes:
            reasons.append("insufficient independent routes")
        if observation.replicas_after_largest_peer_loss < rung.minimum_surviving_replicas:
            reasons.append("insufficient coverage after largest-peer loss")
        if now_ms - observation.stable_since_ms < rung.minimum_soak_seconds * 1000:
            reasons.append("stability soak is incomplete")
        if observation.p95_first_token_ms > rung.maximum_p95_first_token_ms:
            reasons.append("first-token latency exceeds the rung limit")
        if observation.tokens_per_minute < rung.minimum_tokens_per_minute:
            reasons.append("generation throughput is below the rung minimum")
    return ModelEligibility(model=model, eligible=not reasons, reasons=tuple(reasons))


def select_highest_eligible_model(
    catalog: ModelCatalog,
    observations: Sequence[CapacityObservation],
    *,
    now: Optional[float] = None,
) -> Tuple[Optional[CatalogModel], Tuple[ModelEligibility, ...]]:
    """Select the highest safe rung, preferring its primary over its standby.

    This only selects a manifest for a *new* request. It does not mutate aliases,
    stop workers, or move an in-flight request between manifests.
    """

    now_ms = int((time.time() if now is None else now) * 1000)
    by_digest = {}
    for observation in observations:
        if observation.manifest_digest in by_digest:
            raise ModelCatalogError("capacity observations must contain at most one entry per manifest")
        by_digest[observation.manifest_digest] = observation
    rung_by_id = {rung.rung_id: rung for rung in catalog.rungs}
    evaluations = tuple(
        evaluate_model_eligibility(
            model,
            rung_by_id[model.rung_id],
            by_digest.get(model.manifest_digest),
            now_ms=now_ms,
        )
        for model in catalog.models
    )
    eligibility = {item.model.manifest_digest: item.eligible for item in evaluations}
    for rung in sorted(catalog.rungs, key=lambda item: item.order, reverse=True):
        candidates = sorted(
            (model for model in catalog.models if model.rung_id == rung.rung_id),
            key=lambda item: item.role != "primary",
        )
        for model in candidates:
            if eligibility[model.manifest_digest]:
                return model, evaluations
    return None, evaluations
