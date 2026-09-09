"""Content-addressed model identity for public DRIFT swarms.

``ModelManifest`` intentionally uses only deterministic JSON-compatible values.  The SHA-256
digest of its canonical JSON is both its identity and the source of its DHT namespace.  Legacy
private swarms can continue to use their existing human-selected ``dht_prefix`` values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple, Union

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
_CHECKPOINT_ROLES = {"converted_weight", "quantized_weight", "weight"}
_TOKENIZER_FILENAMES = {
    "added_tokens.json",
    "merges.txt",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "spiece.model",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "vocab.json",
    "vocab.txt",
}
_CHECKPOINT_INDEX_PREFERENCE = ("model.safetensors.index.json", "pytorch_model.bin.index.json")
_CHECKPOINT_PREFERENCE = ("model.safetensors", "pytorch_model.bin")


class ManifestError(ValueError):
    """A manifest is malformed, incompatible, or does not match its artifacts."""


class ManifestTransferInterrupted(ManifestError):
    """An immutable artifact transfer stopped before verification and may be resumed."""


_WINDOWS_SHARING_VIOLATION = 32
_VERIFIED_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0)


def _replace_verified_artifact(source: Path, destination: Path) -> None:
    """Promote a verified artifact after transient Windows scanners release the file."""
    for delay in (*_VERIFIED_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) != _WINDOWS_SHARING_VIOLATION or delay is None:
                raise
            time.sleep(delay)


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

    def validate_artifact_layout(self, root: Path | str) -> None:
        """Verify every declared artifact is a regular file with the exact declared size."""
        for artifact in self.artifacts:
            _validate_artifact_file(artifact, _artifact_path_below_root(root, artifact.path))

    def verify_artifacts(self, root: Path | str) -> None:
        """Verify every declared artifact below ``root`` by both size and SHA-256."""
        for artifact in self.artifacts:
            _verify_artifact_file(artifact, _artifact_path_below_root(root, artifact.path))

    def get_artifact(self, path: str) -> ManifestArtifact:
        normalized = PurePosixPath(path).as_posix()
        for artifact in self.artifacts:
            if artifact.path == normalized:
                return artifact
        raise ManifestError(f"Artifact {normalized!r} is not declared by manifest {self.digest_id}")

    def artifacts_for_roles(self, roles: Iterable[str]) -> Tuple[ManifestArtifact, ...]:
        requested = set(roles)
        return tuple(artifact for artifact in self.artifacts if artifact.role in requested)

    def validate_runtime(self, version: str) -> None:
        if not self.runtime.supports(version):
            raise ManifestError(
                f"Manifest requires drift>={self.runtime.minimum_version},"
                f"<{self.runtime.maximum_version_exclusive}; local version is {version}"
            )

    def validate_model_config(self, config: Any) -> None:
        """Reject a downloaded config that does not describe the manifested model shape."""
        architectures = tuple(
            getattr(config, "_source_architectures", None) or getattr(config, "architectures", ()) or ()
        )
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


def _artifact_path_below_root(root: Path | str, relative_path: str) -> Path:
    # Keep Hub snapshot symlinks lexical: resolving them would jump into the shared blob store even though
    # the declared path itself is safely below the snapshot directory.
    root = Path(root).absolute()
    candidate = root.joinpath(*PurePosixPath(relative_path).parts).absolute()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"Artifact escapes verification root: {relative_path}") from exc
    return candidate


def _windows_safe_path(path: Path) -> Path:
    """Opt long manifest-cache paths into the Win32 extended namespace.

    Full manifest and artifact SHA-256 identifiers make resumable lock and
    partial paths exceed the legacy Win32 path limit under an ordinary user
    profile. Python then reports a misleading ``FileNotFoundError`` even when
    the parent directory exists. Keep the audited on-disk layout unchanged,
    but use an extended-length spelling for filesystem operations.
    """
    absolute = path.absolute()
    if os.name != "nt":
        return absolute
    rendered = str(absolute)
    if rendered.startswith("\\\\?\\") or len(rendered) < 248:
        return absolute
    if rendered.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + rendered[2:])
    return Path("\\\\?\\" + rendered)


def _validate_artifact_file(artifact: ManifestArtifact, candidate: Path) -> os.stat_result:
    try:
        stat_result = candidate.stat()
    except OSError as exc:
        raise ManifestError(f"Could not read artifact {artifact.path}: {exc}") from exc
    if not candidate.is_file():
        raise ManifestError(f"Artifact {artifact.path} is not a regular file")
    if stat_result.st_size != artifact.size:
        raise ManifestError(f"Artifact {artifact.path} has size {stat_result.st_size}, expected {artifact.size}")
    return stat_result


def _verify_artifact_file(artifact: ManifestArtifact, candidate: Path) -> os.stat_result:
    stat_result = _validate_artifact_file(artifact, candidate)
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestError(f"Could not read artifact {artifact.path}: {exc}") from exc
    if digest.hexdigest() != artifact.sha256:
        raise ManifestError(f"Artifact {artifact.path} does not match its declared SHA-256")
    return stat_result


@dataclass
class ManifestArtifactVerifier:
    """Download declared artifacts at the pinned revision and verify them before use.

    A verifier deliberately materializes only requested files. This preserves Petals' partial-checkpoint
    behavior: a worker verifies the shards containing its assigned blocks, while an API client verifies the
    tokenizer and the shards containing its local embeddings/head. Successful hashes are cached only while
    the resolved path, size, and modification timestamp remain unchanged.
    """

    manifest: ModelManifest
    repository: str
    revision: str
    token: Optional[Union[str, bool]] = None
    cache_dir: Optional[Union[str, os.PathLike]] = None
    max_disk_space: Optional[int] = None
    artifact_root: Optional[Union[str, os.PathLike]] = None
    _verified: Dict[Tuple[str, int, int, str], bool] = field(default_factory=dict, init=False, repr=False)
    _snapshot_root: Optional[Path] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.repository != self.manifest.source.repository:
            raise ManifestError(
                f"Artifact verifier repository is {self.repository!r}, expected {self.manifest.source.repository!r}"
            )
        if self.revision != self.manifest.source.revision:
            raise ManifestError(
                f"Artifact verifier revision is {self.revision!r}, expected {self.manifest.source.revision!r}"
            )
        if self.artifact_root is not None:
            self._snapshot_root = Path(self.artifact_root).absolute()
        elif self.cache_dir is None:
            from drift.utils.disk_cache import DEFAULT_CACHE_DIR

            self.cache_dir = DEFAULT_CACHE_DIR

    @property
    def snapshot_root(self) -> Path:
        if self._snapshot_root is None:
            raise ManifestError("No manifest artifacts have been materialized yet")
        return self._snapshot_root

    def ensure_startup_metadata(self, *, include_tokenizer: bool = False) -> Path:
        roles: Set[str] = {"config", "weight_index"}
        if include_tokenizer:
            roles.update(("chat_template", "tokenizer"))
        artifacts = self.manifest.artifacts_for_roles(roles)
        if not any(artifact.role == "config" for artifact in artifacts):
            raise ManifestError("Manifest has no configuration artifact")
        for artifact in artifacts:
            self.ensure_path(artifact.path, allowed_roles=roles)
        return self.snapshot_root

    def ensure_path(self, path: str, *, allowed_roles: Optional[Iterable[str]] = None) -> Path:
        artifact = self.manifest.get_artifact(path)
        if allowed_roles is not None and artifact.role not in set(allowed_roles):
            raise ManifestError(
                f"Artifact {artifact.path!r} has role {artifact.role!r}, expected one of {sorted(set(allowed_roles))}"
            )

        candidate = None
        if self._snapshot_root is not None:
            candidate = _artifact_path_below_root(self._snapshot_root, artifact.path)
        if candidate is None or (not candidate.exists() and self.artifact_root is None):
            try:
                from huggingface_hub import hf_hub_download
                from huggingface_hub.utils import LocalEntryNotFoundError

                try:
                    resolved = hf_hub_download(
                        self.repository,
                        artifact.path,
                        revision=self.revision,
                        token=self.token,
                        cache_dir=self.cache_dir,
                        local_files_only=True,
                    )
                except LocalEntryNotFoundError:
                    resolved = None

                if resolved is None:
                    from drift.utils.disk_cache import allow_cache_writes, free_disk_space_for

                    Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
                    with allow_cache_writes(self.cache_dir):
                        free_disk_space_for(
                            artifact.size,
                            cache_dir=self.cache_dir,
                            max_disk_space=self.max_disk_space,
                        )
                        resolved = self._resumable_hub_download(artifact, destination=candidate)
            except ManifestTransferInterrupted:
                raise
            except Exception as exc:
                raise ManifestError(
                    f"Could not materialize declared artifact {artifact.path} from "
                    f"{self.repository}@{self.revision}: {exc}"
                ) from exc
            resolved_candidate = Path(resolved).absolute()
            resolved_root = resolved_candidate
            for _ in PurePosixPath(artifact.path).parts:
                resolved_root = resolved_root.parent
            if self._snapshot_root is None:
                candidate = resolved_candidate
                self._snapshot_root = resolved_root
            else:
                if candidate is None:  # pragma: no cover - maintained by the snapshot-root invariant
                    raise ManifestError("Manifest verifier lost its snapshot root")
                if resolved_candidate != candidate:
                    self._promote_cached_artifact(artifact, resolved_candidate, candidate)

        self._verify(artifact, candidate)
        return candidate

    def _promote_cached_artifact(self, artifact: ManifestArtifact, source: Path, destination: Path) -> None:
        """Materialize a verified cached file into the verifier's single snapshot root."""
        from drift.utils.file_lock import file_lock

        _, _, lock = self._resumable_paths(artifact)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{artifact.sha256[:12]}.tmp")
        with file_lock(lock, exclusive=True):
            if destination.exists():
                _verify_artifact_file(artifact, destination)
                return
            _verify_artifact_file(artifact, source)
            if temporary.exists():
                temporary.unlink()
            try:
                try:
                    os.link(source, temporary)
                except OSError:
                    with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                _verify_artifact_file(artifact, temporary)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def partial_size(self, path: str) -> int:
        """Return resumable bytes retained for one declared artifact, without exposing its local path."""
        artifact = self.manifest.get_artifact(path)
        if self.cache_dir is None:
            return 0
        partial, _, _ = self._resumable_paths(artifact)
        try:
            return partial.stat().st_size if partial.is_file() and not partial.is_symlink() else 0
        except OSError:
            return 0

    def _resumable_paths(self, artifact: ManifestArtifact) -> Tuple[Path, Path, Path]:
        """Return deterministic partial, final, and lock paths for one manifested artifact."""
        cache_root = Path(self.cache_dir).absolute()
        manifest_root = cache_root / "manifest-artifacts" / self.manifest.digest
        name_digest = hashlib.sha256(artifact.path.encode("utf-8")).hexdigest()
        partial = _windows_safe_path(manifest_root / "partial" / f"{name_digest}.part")
        final = _windows_safe_path(_artifact_path_below_root(manifest_root / "snapshot", artifact.path))
        lock = _windows_safe_path(manifest_root / "locks" / f"{name_digest}.lock")
        return partial, final, lock

    def _resumable_hub_download(self, artifact: ManifestArtifact, *, destination: Optional[Path] = None) -> str:
        """Download one immutable Hub artifact with verified HTTP Range resumption.

        huggingface_hub 1.x deliberately deletes process-unique partial files after an
        interrupted transfer, so the manifest path owns a small content-addressed cache.
        A partial is never exposed to Transformers.  It is atomically promoted only after
        its exact declared size and SHA-256 pass.
        """
        import requests
        from huggingface_hub import hf_hub_url
        from huggingface_hub.utils import build_hf_headers

        from drift.utils.file_lock import file_lock

        partial, default_final, lock = self._resumable_paths(artifact)
        final = default_final if destination is None else destination.absolute()
        partial.parent.mkdir(parents=True, exist_ok=True)
        final.parent.mkdir(parents=True, exist_ok=True)

        with file_lock(lock, exclusive=True):
            if final.exists():
                _verify_artifact_file(artifact, final)
                return str(final)

            if partial.exists() and partial.stat().st_size > artifact.size:
                partial.unlink()
            offset = partial.stat().st_size if partial.exists() else 0
            if offset == artifact.size:
                try:
                    _verify_artifact_file(artifact, partial)
                except ManifestError:
                    partial.unlink()
                    offset = 0
                else:
                    _replace_verified_artifact(partial, final)
                    return str(final)

            url = hf_hub_url(self.repository, artifact.path, revision=self.revision)
            headers = build_hf_headers(token=self.token, library_name="drift", library_version="2")
            if offset:
                headers["Range"] = f"bytes={offset}-"

            try:
                response = requests.get(url, headers=headers, stream=True, allow_redirects=True, timeout=(10, 60))
                response.raise_for_status()
                if offset and response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise ManifestError(
                            f"Hub returned an invalid Content-Range while resuming {artifact.path}: {content_range!r}"
                        )
                    mode = "ab"
                else:
                    # A 200 response means the origin ignored Range; restart safely instead of appending.
                    offset = 0
                    mode = "wb"

                with partial.open(mode) as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            except (OSError, requests.RequestException) as exc:
                # Keep a bounded partial for a later Range request, but never expose it as a model file.
                if partial.exists() and partial.stat().st_size > artifact.size:
                    partial.unlink()
                raise ManifestTransferInterrupted(
                    f"Interrupted download of {artifact.path} at byte {offset}: {type(exc).__name__}"
                ) from exc
            finally:
                if "response" in locals():
                    response.close()

            try:
                _verify_artifact_file(artifact, partial)
            except ManifestError:
                # A completed but invalid transfer must not poison all later retries.
                if partial.exists() and partial.stat().st_size >= artifact.size:
                    partial.unlink()
                raise
            _replace_verified_artifact(partial, final)
            return str(final)

    def verify_resolved_file(
        self, path: Union[str, os.PathLike], *, allowed_roles: Optional[Iterable[str]] = None
    ) -> Path:
        candidate = Path(path).absolute()
        matches = []
        candidate_parts = candidate.parts
        for artifact in self.manifest.artifacts:
            artifact_parts = PurePosixPath(artifact.path).parts
            if len(candidate_parts) >= len(artifact_parts) and tuple(candidate_parts[-len(artifact_parts) :]) == tuple(
                artifact_parts
            ):
                matches.append(artifact)
        if len(matches) != 1:
            raise ManifestError(
                f"Resolved checkpoint file {candidate} does not map uniquely to a declared manifest artifact"
            )
        artifact = matches[0]
        if allowed_roles is not None and artifact.role not in set(allowed_roles):
            raise ManifestError(
                f"Resolved file {artifact.path!r} has role {artifact.role!r}, expected one of "
                f"{sorted(set(allowed_roles))}"
            )
        self._verify(artifact, candidate)
        return candidate

    def verify_checkpoint_files(self, paths: Sequence[Union[str, os.PathLike]]) -> None:
        if not paths:
            raise ManifestError("Model loader resolved no checkpoint artifacts")
        for path in paths:
            self.verify_resolved_file(path, allowed_roles=_CHECKPOINT_ROLES)

    def _verify(self, artifact: ManifestArtifact, candidate: Path) -> None:
        try:
            stat_result = candidate.stat()
        except OSError as exc:
            raise ManifestError(f"Could not read artifact {artifact.path}: {exc}") from exc
        cache_key = (str(candidate), stat_result.st_size, stat_result.st_mtime_ns, artifact.sha256)
        if cache_key in self._verified:
            return
        _verify_artifact_file(artifact, candidate)
        self._verified = {key: value for key, value in self._verified.items() if key[0] != str(candidate)}
        self._verified[cache_key] = True


def _select_snapshot_artifacts(files: Iterable[str], root: Path | str) -> Dict[str, str]:
    """Select the deterministic Transformers artifact set from one local Hub snapshot."""
    root = Path(root)
    normalized_files = {PurePosixPath(path).as_posix() for path in files}
    if "config.json" not in normalized_files:
        raise ManifestError("Snapshot does not contain config.json")

    selected = {"config.json": "config"}
    for path in sorted(normalized_files):
        pure_path = PurePosixPath(path)
        if pure_path.name in _TOKENIZER_FILENAMES:
            selected[path] = "tokenizer"
        elif (path == "chat_template.jinja" or path.startswith("chat_templates/")) and path.endswith(".jinja"):
            selected[path] = "chat_template"
    if "tokenizer" not in set(selected.values()):
        raise ManifestError("Snapshot does not contain a recognized tokenizer artifact")

    index_path = next((path for path in _CHECKPOINT_INDEX_PREFERENCE if path in normalized_files), None)
    if index_path is not None:
        selected[index_path] = "weight_index"
        try:
            index = json.loads(_artifact_path_below_root(root, index_path).read_text(encoding="utf-8"))
            weight_map = index["weight_map"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ManifestError(f"Could not read checkpoint index {index_path}: {exc}") from exc
        if not isinstance(weight_map, dict) or not weight_map:
            raise ManifestError(f"Checkpoint index {index_path} has no non-empty weight_map")
        weight_paths = set(weight_map.values())
        if any(not isinstance(path, str) for path in weight_paths):
            raise ManifestError(f"Checkpoint index {index_path} contains a non-string shard path")
        weight_paths = sorted(weight_paths)
        missing = set(weight_paths) - normalized_files
        if missing:
            raise ManifestError(f"Checkpoint index {index_path} references missing shards: {sorted(missing)}")
        selected.update((path, "weight") for path in weight_paths)
    else:
        checkpoint_path = next((path for path in _CHECKPOINT_PREFERENCE if path in normalized_files), None)
        if checkpoint_path is None:
            raise ManifestError(
                "Snapshot contains no supported checkpoint; expected safetensors or PyTorch weights/index"
            )
        selected[checkpoint_path] = "weight"
    return selected


def create_manifest_from_snapshot(
    *,
    repository: str,
    revision: str,
    artifact_root: Path | str,
    name: str,
    aliases: Sequence[str],
    license_name: str,
    gated: bool,
    minimum_version: str = "2.3.0.dev0",
    maximum_version_exclusive: str = "2.4.0",
    attention_implementation: str = "auto",
    dtype: str = "bfloat16",
    quantization: str = "none",
) -> ModelManifest:
    """Create a deterministic v1 manifest from a complete, immutable local Hub snapshot."""
    artifact_root = Path(artifact_root).resolve()
    files = sorted(path.relative_to(artifact_root).as_posix() for path in artifact_root.rglob("*") if path.is_file())
    selected = _select_snapshot_artifacts(files, artifact_root)

    try:
        config = json.loads(_artifact_path_below_root(artifact_root, "config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Could not read config.json: {exc}") from exc
    text_config = config.get("text_config", config)
    architectures = text_config.get("architectures") or config.get("architectures")
    architecture = architectures[0] if isinstance(architectures, list) and architectures else None
    num_blocks = text_config.get("num_hidden_layers")
    context_length = text_config.get("max_position_embeddings")
    if not isinstance(architecture, str) or not architecture:
        raise ManifestError("config.json does not declare a usable architectures[0]")
    if isinstance(num_blocks, bool) or not isinstance(num_blocks, int) or num_blocks < 1:
        raise ManifestError("config.json does not declare a positive num_hidden_layers")
    if isinstance(context_length, bool) or not isinstance(context_length, int) or context_length < 1:
        raise ManifestError("config.json does not declare a positive max_position_embeddings")

    artifacts = []
    for path, role in sorted(selected.items()):
        candidate = _artifact_path_below_root(artifact_root, path)
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        artifacts.append({"role": role, "path": path, "sha256": digest.hexdigest(), "size": candidate.stat().st_size})

    return ModelManifest.from_dict(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "name": name,
            "aliases": list(aliases),
            "source": {"repository": repository, "revision": revision},
            "model": {
                "architecture": architecture,
                "num_blocks": num_blocks,
                "context_length": context_length,
                "license": license_name,
                "gated": gated,
            },
            "runtime": {
                "implementation": "drift",
                "minimum_version": minimum_version,
                "maximum_version_exclusive": maximum_version_exclusive,
                "protocol_version": MANIFEST_PROTOCOL_VERSION,
                "tensor_schema": "hidden-states-v1",
                "attention_implementation": attention_implementation,
                "dtype": dtype,
                "quantization": quantization,
                "adapter_profile": "none",
            },
            "artifacts": artifacts,
        }
    )
