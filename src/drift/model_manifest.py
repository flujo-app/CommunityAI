"""Content-addressed model identity for public DRIFT swarms.

``ModelManifest`` intentionally uses only deterministic JSON-compatible values.  The SHA-256
digest of its canonical JSON is both its identity and the source of its DHT namespace.  Legacy
private swarms can continue to use their existing human-selected ``dht_prefix`` values.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from packaging.version import InvalidVersion, Version

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_PROTOCOL_VERSION = 1
MANIFEST_NAMESPACE_PREFIX = "drift-m1-"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_ROLES = {
    "chat_template",
    "config",
    "converted_weight",
    "quantized_weight",
    "tokenizer",
    "weight",
    "weight_index",
}
_DTYPES = {"bfloat16", "float16", "float32"}
_QUANTIZATIONS = {"int8", "nf4", "none"}
_ATTENTION_IMPLEMENTATIONS = {"auto", "eager", "sdpa"}


class ManifestError(ValueError):
    """A manifest is malformed, incompatible, or does not match its artifacts."""


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field} must be a JSON object")
    return value


def _require_keys(value: Mapping[str, Any], field: str, required: Iterable[str]) -> None:
    required = set(required)
    actual = set(value)
    missing, extra = required - actual, actual - required
    if missing:
        raise ManifestError(f"{field} is missing required field(s): {', '.join(sorted(missing))}")
    if extra:
        raise ManifestError(f"{field} has unknown field(s): {', '.join(sorted(extra))}")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ManifestError(f"{field} must use NFC-normalized Unicode")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ManifestError(f"{field} must contain valid Unicode scalar values") from exc
    return value


def _require_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestError(f"{field} must be an integer >= {minimum}")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{field} must be a boolean")
    return value


def _require_string_list(value: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"{field} must be a JSON array")
    result = tuple(_require_string(item, f"{field}[]") for item in value)
    if len(set(result)) != len(result):
        raise ManifestError(f"{field} must not contain duplicates")
    return result


@dataclass(frozen=True)
class ManifestArtifact:
    role: str
    path: str
    sha256: str
    size: int

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "ManifestArtifact":
        source = _require_object(source, "artifacts[]")
        _require_keys(source, "artifacts[]", ("role", "path", "sha256", "size"))

        role = _require_string(source["role"], "artifacts[].role")
        if role not in _ARTIFACT_ROLES:
            raise ManifestError(f"artifacts[].role must be one of {sorted(_ARTIFACT_ROLES)}, got {role!r}")

        path = _require_string(source["path"], "artifacts[].path")
        parsed_path = PurePosixPath(path)
        if (
            parsed_path.is_absolute()
            or parsed_path == PurePosixPath(".")
            or "\\" in path
            or path != parsed_path.as_posix()
            or ".." in parsed_path.parts
        ):
            raise ManifestError(f"artifacts[].path must be a normalized relative POSIX path, got {path!r}")

        sha256 = _require_string(source["sha256"], "artifacts[].sha256")
        if not _SHA256_RE.fullmatch(sha256):
            raise ManifestError("artifacts[].sha256 must be 64 lowercase hexadecimal characters")

        return cls(
            role=role, path=path, sha256=sha256, size=_require_int(source["size"], "artifacts[].size", minimum=0)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class ModelSource:
    repository: str
    revision: str

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "ModelSource":
        source = _require_object(source, "source")
        _require_keys(source, "source", ("repository", "revision"))
        repository = _require_string(source["repository"], "source.repository")
        revision = _require_string(source["revision"], "source.revision")
        if not _GIT_REVISION_RE.fullmatch(revision):
            raise ManifestError("source.revision must be a full 40-character lowercase Git commit SHA")
        return cls(repository=repository, revision=revision)

    def to_dict(self) -> Dict[str, Any]:
        return {"repository": self.repository, "revision": self.revision}


@dataclass(frozen=True)
class ModelDescription:
    architecture: str
    num_blocks: int
    context_length: int
    license: str
    gated: bool

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "ModelDescription":
        source = _require_object(source, "model")
        _require_keys(source, "model", ("architecture", "num_blocks", "context_length", "license", "gated"))
        return cls(
            architecture=_require_string(source["architecture"], "model.architecture"),
            num_blocks=_require_int(source["num_blocks"], "model.num_blocks"),
            context_length=_require_int(source["context_length"], "model.context_length"),
            license=_require_string(source["license"], "model.license"),
            gated=_require_bool(source["gated"], "model.gated"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "architecture": self.architecture,
            "num_blocks": self.num_blocks,
            "context_length": self.context_length,
            "license": self.license,
            "gated": self.gated,
        }


@dataclass(frozen=True)
class RuntimeProfile:
    implementation: str
    minimum_version: str
    maximum_version_exclusive: str
    protocol_version: int
    tensor_schema: str
    attention_implementation: str
    dtype: str
    quantization: str
    adapter_profile: str

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "RuntimeProfile":
        source = _require_object(source, "runtime")
        fields = (
            "implementation",
            "minimum_version",
            "maximum_version_exclusive",
            "protocol_version",
            "tensor_schema",
            "attention_implementation",
            "dtype",
            "quantization",
            "adapter_profile",
        )
        _require_keys(source, "runtime", fields)
        result = cls(
            implementation=_require_string(source["implementation"], "runtime.implementation"),
            minimum_version=_require_string(source["minimum_version"], "runtime.minimum_version"),
            maximum_version_exclusive=_require_string(
                source["maximum_version_exclusive"], "runtime.maximum_version_exclusive"
            ),
            protocol_version=_require_int(source["protocol_version"], "runtime.protocol_version"),
            tensor_schema=_require_string(source["tensor_schema"], "runtime.tensor_schema"),
            attention_implementation=_require_string(
                source["attention_implementation"], "runtime.attention_implementation"
            ),
            dtype=_require_string(source["dtype"], "runtime.dtype"),
            quantization=_require_string(source["quantization"], "runtime.quantization"),
            adapter_profile=_require_string(source["adapter_profile"], "runtime.adapter_profile"),
        )
        result._validate()
        return result

    def _validate(self) -> None:
        if self.implementation != "drift":
            raise ManifestError("runtime.implementation must be 'drift' for ModelManifest v1")
        if self.protocol_version != MANIFEST_PROTOCOL_VERSION:
            raise ManifestError(f"runtime.protocol_version must be {MANIFEST_PROTOCOL_VERSION}")
        if self.tensor_schema != "hidden-states-v1":
            raise ManifestError("runtime.tensor_schema must be 'hidden-states-v1' for ModelManifest v1")
        if self.attention_implementation not in _ATTENTION_IMPLEMENTATIONS:
            raise ManifestError(f"runtime.attention_implementation must be one of {sorted(_ATTENTION_IMPLEMENTATIONS)}")
        if self.dtype not in _DTYPES:
            raise ManifestError(f"runtime.dtype must be one of {sorted(_DTYPES)}")
        if self.quantization not in _QUANTIZATIONS:
            raise ManifestError(f"runtime.quantization must be one of {sorted(_QUANTIZATIONS)}")
        if self.adapter_profile != "none" and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.adapter_profile):
            raise ManifestError("runtime.adapter_profile must be 'none' or a sha256:<digest> profile identifier")
        try:
            minimum, maximum = Version(self.minimum_version), Version(self.maximum_version_exclusive)
        except InvalidVersion as exc:
            raise ManifestError(f"runtime version bounds must be valid PEP 440 versions: {exc}") from exc
        if minimum >= maximum:
            raise ManifestError("runtime.minimum_version must be less than runtime.maximum_version_exclusive")

    def supports(self, version: str) -> bool:
        try:
            candidate = Version(version)
        except InvalidVersion as exc:
            raise ManifestError(f"Invalid local DRIFT version {version!r}: {exc}") from exc
        return Version(self.minimum_version) <= candidate < Version(self.maximum_version_exclusive)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "implementation": self.implementation,
            "minimum_version": self.minimum_version,
            "maximum_version_exclusive": self.maximum_version_exclusive,
            "protocol_version": self.protocol_version,
            "tensor_schema": self.tensor_schema,
            "attention_implementation": self.attention_implementation,
            "dtype": self.dtype,
            "quantization": self.quantization,
            "adapter_profile": self.adapter_profile,
        }


@dataclass(frozen=True)
class ModelManifest:
    schema_version: int
    name: str
    aliases: Tuple[str, ...]
    source: ModelSource
    model: ModelDescription
    runtime: RuntimeProfile
    artifacts: Tuple[ManifestArtifact, ...]

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "ModelManifest":
        source = _require_object(source, "manifest")
        _require_keys(
            source, "manifest", ("schema_version", "name", "aliases", "source", "model", "runtime", "artifacts")
        )
        schema_version = _require_int(source["schema_version"], "schema_version")
        if schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(f"Unsupported schema_version {schema_version}; expected {MANIFEST_SCHEMA_VERSION}")

        artifacts_value = source["artifacts"]
        if not isinstance(artifacts_value, list):
            raise ManifestError("artifacts must be a JSON array")
        artifacts = tuple(ManifestArtifact.from_dict(item) for item in artifacts_value)
        paths = [artifact.path for artifact in artifacts]
        if len(set(paths)) != len(paths):
            raise ManifestError("artifacts must not contain duplicate paths")
        roles = {artifact.role for artifact in artifacts}
        missing_roles = {"config", "tokenizer"} - roles
        if not ({"weight", "converted_weight", "quantized_weight"} & roles):
            missing_roles.add("weight, converted_weight, or quantized_weight")
        if missing_roles:
            raise ManifestError(f"artifacts are missing required role(s): {', '.join(sorted(missing_roles))}")

        result = cls(
            schema_version=schema_version,
            name=_require_string(source["name"], "name"),
            aliases=_require_string_list(source["aliases"], "aliases"),
            source=ModelSource.from_dict(source["source"]),
            model=ModelDescription.from_dict(source["model"]),
            runtime=RuntimeProfile.from_dict(source["runtime"]),
            artifacts=artifacts,
        )
        names = (result.name, *result.aliases)
        if len({name.casefold() for name in names}) != len(names):
            raise ManifestError("name and aliases must be unique when compared case-insensitively")
        return result

    @classmethod
    def from_json(cls, source: str) -> "ModelManifest":
        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ManifestError(f"Manifest JSON contains duplicate object key {key!r}")
                result[key] = value
            return result

        def reject_non_finite(value):
            raise ManifestError(f"Manifest JSON contains non-finite number {value}")

        try:
            value = json.loads(source, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"Invalid manifest JSON: {exc}") from exc
        return cls.from_dict(value)

    @classmethod
    def load(cls, path: Path | str) -> "ModelManifest":
        path = Path(path)
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ManifestError(f"Could not read manifest {path}: {exc}") from exc

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "aliases": sorted(self.aliases),
            "source": self.source.to_dict(),
            "model": self.model.to_dict(),
            "runtime": self.runtime.to_dict(),
            "artifacts": [
                artifact.to_dict() for artifact in sorted(self.artifacts, key=lambda item: (item.path, item.role))
            ],
        }

    def canonical_json(self) -> str:
        """Return the exact UTF-8 JSON text hashed by :attr:`digest`."""
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def digest_id(self) -> str:
        return f"sha256:{self.digest}"

    @property
    def dht_prefix(self) -> str:
        return f"{MANIFEST_NAMESPACE_PREFIX}{self.digest}"

    def verify_artifacts(self, root: Path | str) -> None:
        """Verify every declared artifact below ``root`` by both size and SHA-256."""
        root = Path(root).resolve()
        for artifact in self.artifacts:
            candidate = root.joinpath(*PurePosixPath(artifact.path).parts).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ManifestError(f"Artifact escapes verification root: {artifact.path}") from exc
            try:
                size = candidate.stat().st_size
            except OSError as exc:
                raise ManifestError(f"Could not read artifact {artifact.path}: {exc}") from exc
            if size != artifact.size:
                raise ManifestError(f"Artifact {artifact.path} has size {size}, expected {artifact.size}")
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise ManifestError(f"Could not read artifact {artifact.path}: {exc}") from exc
            if digest.hexdigest() != artifact.sha256:
                raise ManifestError(f"Artifact {artifact.path} does not match its declared SHA-256")

    def validate_runtime(self, version: str) -> None:
        if not self.runtime.supports(version):
            raise ManifestError(
                f"Manifest requires drift>={self.runtime.minimum_version},"
                f"<{self.runtime.maximum_version_exclusive}; local version is {version}"
            )

    def validate_model_config(self, config: Any) -> None:
        """Reject a downloaded config that does not describe the manifested model shape."""
        architectures = tuple(getattr(config, "architectures", ()) or ())
        if self.model.architecture not in architectures:
            raise ManifestError(
                f"Manifest architecture {self.model.architecture!r} does not match config architectures {architectures!r}"
            )
        num_blocks = getattr(config, "num_hidden_layers", None)
        if num_blocks != self.model.num_blocks:
            raise ManifestError(f"Manifest declares {self.model.num_blocks} blocks but config declares {num_blocks!r}")
        context_length = getattr(config, "max_position_embeddings", None)
        if context_length != self.model.context_length:
            raise ManifestError(
                f"Manifest declares context length {self.model.context_length} but config declares {context_length!r}"
            )


def resolve_manifest_loading(
    manifest: ModelManifest,
    *,
    model_name_or_path: str,
    revision: Optional[str],
    dht_prefix: Optional[str],
) -> Tuple[str, str]:
    """Validate CLI loading inputs and return the pinned revision and content-derived prefix."""
    if model_name_or_path != manifest.source.repository:
        raise ManifestError(
            f"Manifest repository is {manifest.source.repository!r}, but the requested model is {model_name_or_path!r}"
        )
    if revision is not None and revision != manifest.source.revision:
        raise ManifestError(f"--revision {revision!r} conflicts with manifest revision {manifest.source.revision!r}")
    if dht_prefix is not None and dht_prefix != manifest.dht_prefix:
        raise ManifestError(f"--dht_prefix {dht_prefix!r} conflicts with manifest namespace {manifest.dht_prefix!r}")
    return manifest.source.revision, manifest.dht_prefix
