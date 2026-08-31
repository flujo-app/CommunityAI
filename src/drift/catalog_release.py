"""Fail-closed checks for catalog/bootstrap artifacts before publication.

This module validates the repository-controlled portion of a catalog release. It
cannot prove who operates a seed or mirror, real worker coverage, cross-platform
qualification, or packaged inference; those remain external release gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence
from urllib.parse import urlsplit

from drift.model_catalog import ModelCatalogError, SignedModelCatalog
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapError

PUBLICATION_PREFLIGHT_SCHEMA_VERSION = 1
MAX_PUBLICATION_PREFLIGHT_BYTES = 1024 * 1024
CATALOG_PUBLICATION_BUNDLE_SCHEMA_VERSION = 1
CATALOG_PUBLICATION_BUNDLE_INDEX_NAME = "bundle.json"
MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_CATALOG_PUBLICATION_BUNDLE_TOTAL_BYTES = 64 * 1024 * 1024
_SEED_HOST_PROTOCOLS = {"dns", "dns4", "dns6", "ip4", "ip6"}
_MANIFEST_BUNDLE_PATH = re.compile(r"manifests/([0-9a-f]{64})\.json\Z")
_FIXED_BUNDLE_MEMBER_PATHS = {
    "catalog-bootstrap.json",
    "catalog.signed.json",
    "publication-preflight.json",
}
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def catalog_bootstrap_digest(bootstrap: CatalogBootstrapConfig) -> str:
    """Return the canonical digest used to bind preflight evidence to a bootstrap."""

    canonical = json.dumps(
        bootstrap.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def verify_catalog_publication_preflight_report(
    bootstrap: CatalogBootstrapConfig, report: Mapping[str, Any]
) -> Dict[str, Any]:
    """Validate and reduce a machine-readable publication-preflight report."""

    if not isinstance(report, dict):
        raise CatalogBootstrapError("Publication preflight report must be a JSON object")
    schema_version = report.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PUBLICATION_PREFLIGHT_SCHEMA_VERSION
    ):
        raise CatalogBootstrapError(
            f"Unsupported publication preflight schema version {schema_version!r}; "
            f"expected {PUBLICATION_PREFLIGHT_SCHEMA_VERSION}"
        )
    if report.get("scope") != "catalog-publication-transport-preflight":
        raise CatalogBootstrapError("Publication preflight report has an unexpected scope")
    if report.get("result") != "passed":
        raise CatalogBootstrapError("Publication preflight report did not pass")
    if report.get("complete_release_qualification") is not False:
        raise CatalogBootstrapError("Publication preflight must explicitly retain incomplete release qualification")

    catalog_id = report.get("catalog_id")
    if catalog_id != bootstrap.trust_root.catalog_id:
        raise CatalogBootstrapError("Publication preflight catalog_id does not match the bootstrap trust root")
    catalog_sequence = report.get("catalog_sequence")
    if isinstance(catalog_sequence, bool) or not isinstance(catalog_sequence, int) or catalog_sequence < 1:
        raise CatalogBootstrapError("Publication preflight catalog_sequence must be an integer >= 1")
    catalog_digest = report.get("catalog_digest")
    if not isinstance(catalog_digest, str) or _SHA256_DIGEST.fullmatch(catalog_digest) is None:
        raise CatalogBootstrapError("Publication preflight catalog_digest must be a canonical SHA-256 digest")
    bootstrap_digest = report.get("bootstrap_digest")
    expected_bootstrap_digest = catalog_bootstrap_digest(bootstrap)
    if bootstrap_digest != expected_bootstrap_digest:
        raise CatalogBootstrapError("Publication preflight does not match the exact bootstrap config")

    return {
        "schema_version": schema_version,
        "scope": report["scope"],
        "result": report["result"],
        "catalog_id": catalog_id,
        "catalog_sequence": catalog_sequence,
        "catalog_digest": catalog_digest,
        "bootstrap_digest": bootstrap_digest,
        "complete_release_qualification": False,
    }


def load_catalog_publication_preflight_report(bootstrap: CatalogBootstrapConfig, path: Path | str) -> Dict[str, Any]:
    """Load a bounded, strict JSON report and bind it to the exact bootstrap."""

    resolved = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    if not resolved.is_file() or resolved.is_symlink():
        raise CatalogBootstrapError(f"Publication preflight report is missing or unsafe: {resolved}")
    try:
        size = resolved.stat().st_size
        if size < 1 or size > MAX_PUBLICATION_PREFLIGHT_BYTES:
            raise CatalogBootstrapError(
                f"Publication preflight report must be between 1 and {MAX_PUBLICATION_PREFLIGHT_BYTES} bytes"
            )
        source = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CatalogBootstrapError(f"Could not read publication preflight report {resolved}: {exc}") from exc

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CatalogBootstrapError(f"Publication preflight report contains duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise CatalogBootstrapError(f"Publication preflight report contains non-finite number {value}")

    try:
        report = json.loads(source, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
    except json.JSONDecodeError as exc:
        raise CatalogBootstrapError(f"Invalid publication preflight report JSON: {exc}") from exc
    return verify_catalog_publication_preflight_report(bootstrap, report)


def _mirror_hosts(bootstrap: CatalogBootstrapConfig) -> tuple[str, ...]:
    hosts = tuple((urlsplit(url).hostname or "").casefold() for url in bootstrap.catalog_mirrors)
    if not hosts:
        raise CatalogBootstrapError("Publication requires at least one catalog mirror")
    if len(set(hosts)) != len(hosts):
        raise CatalogBootstrapError("Publication catalog mirrors must use distinct network hosts")
    return hosts


def _seed_components(bootstrap: CatalogBootstrapConfig) -> tuple[tuple[str, str, str], ...]:
    if not bootstrap.initial_peers:
        raise CatalogBootstrapError("Publication requires at least one public seed")

    components = []
    for peer in bootstrap.initial_peers:
        endpoint, separator, peer_id = peer.rpartition("/p2p/")
        if not separator or not endpoint or not peer_id or "/" in peer_id:
            raise CatalogBootstrapError("Publication seed addresses must end with exactly one /p2p/<peer-id> component")
        address_parts = endpoint.split("/")[1:]
        host = next(
            (
                address_parts[index + 1].casefold()
                for index, protocol in enumerate(address_parts[:-1])
                if protocol in _SEED_HOST_PROTOCOLS and address_parts[index + 1]
            ),
            None,
        )
        if host is None:
            raise CatalogBootstrapError("Publication seed addresses must contain a direct DNS or IP network host")
        components.append((host, endpoint.casefold(), peer_id))

    hosts = [host for host, _, _ in components]
    endpoints = [endpoint for _, endpoint, _ in components]
    peer_ids = [peer_id for _, _, peer_id in components]
    if len(set(hosts)) != len(hosts):
        raise CatalogBootstrapError("Publication seeds must use distinct network hosts")
    if len(set(endpoints)) != len(endpoints):  # pragma: no cover - distinct hosts already imply this
        raise CatalogBootstrapError("Publication seeds must use distinct network addresses")
    if len(set(peer_ids)) != len(peer_ids):
        raise CatalogBootstrapError("Publication seeds must use distinct libp2p peer identities")
    return tuple(components)


def verify_catalog_publication_bundle(
    bootstrap: CatalogBootstrapConfig,
    envelope: SignedModelCatalog,
    manifests: Sequence[ModelManifest],
    *,
    now: float | None = None,
) -> Dict[str, Any]:
    """Validate the locally auditable catalog publication inputs.

    A passing result means the transport/bootstrap documents are internally
    consistent. It intentionally does not set or imply complete release
    qualification.
    """

    mirror_hosts = _mirror_hosts(bootstrap)
    seed_components = _seed_components(bootstrap)
    try:
        catalog = envelope.verify(bootstrap.trust_root, now=now)
    except ModelCatalogError as exc:
        raise CatalogBootstrapError(f"Publication catalog verification failed: {exc}") from exc

    manifest_by_digest: dict[str, ModelManifest] = {}
    for manifest in manifests:
        if manifest.digest_id in manifest_by_digest:
            raise CatalogBootstrapError(f"Publication contains duplicate manifest {manifest.digest_id}")
        manifest_by_digest[manifest.digest_id] = manifest

    expected_digests = {model.manifest_digest for model in catalog.models}
    supplied_digests = set(manifest_by_digest)
    missing = sorted(expected_digests - supplied_digests)
    extra = sorted(supplied_digests - expected_digests)
    if missing:
        raise CatalogBootstrapError(f"Publication is missing catalog manifest(s): {missing}")
    if extra:
        raise CatalogBootstrapError(f"Publication contains manifest(s) absent from the catalog: {extra}")

    selectors: dict[str, str] = {}
    for model in catalog.models:
        manifest = manifest_by_digest[model.manifest_digest]
        declared_weight_bytes = sum(artifact.size for artifact in manifest.artifacts if artifact.role == "weight")
        if model.weight_bytes != declared_weight_bytes:
            raise CatalogBootstrapError(
                f"Catalog weight_bytes for {model.manifest_digest} is {model.weight_bytes}, "
                f"but its exact manifest declares {declared_weight_bytes}"
            )
        for selector in (manifest.name, *manifest.aliases):
            folded = selector.casefold()
            previous_digest = selectors.get(folded)
            if previous_digest is not None and previous_digest != manifest.digest_id:
                raise CatalogBootstrapError(
                    f"Publication manifests reuse model selector {selector!r} across different digests"
                )
            selectors[folded] = manifest.digest_id

    for rung in catalog.rungs:
        if rung.minimum_replicas < 1 or rung.minimum_independent_routes < 1 or rung.minimum_surviving_replicas < 1:
            raise CatalogBootstrapError(
                f"Publication rung {rung.rung_id!r} must require at least one complete replica, "
                "one route, and one surviving replica"
            )

    return {
        "schema_version": PUBLICATION_PREFLIGHT_SCHEMA_VERSION,
        "scope": "catalog-publication-transport-preflight",
        "result": "passed",
        "catalog_id": catalog.catalog_id,
        "catalog_sequence": catalog.sequence,
        "catalog_digest": catalog.digest,
        "bootstrap_digest": catalog_bootstrap_digest(bootstrap),
        "model_count": len(catalog.models),
        "rung_count": len(catalog.rungs),
        "catalog_mirror_count": len(mirror_hosts),
        "distinct_seed_host_count": len({host for host, _, _ in seed_components}),
        "distinct_seed_address_count": len(seed_components),
        "distinct_seed_identity_count": len({peer_id for _, _, peer_id in seed_components}),
        "complete_release_qualification": False,
        "not_covered": [
            "cross-platform and multi-machine model qualification",
            "mirror and seed redundancy or independent operator ownership",
            "public-worker route redundancy and soak",
            "packaged clean-install inference",
        ],
    }


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def catalog_publication_bundle_index_digest(index: Mapping[str, Any]) -> str:
    """Return the canonical digest of a validated publication-bundle index."""

    return _sha256_digest_bytes(_canonical_json_bytes(index))


def _load_strict_json_document(source: bytes, *, name: str) -> Dict[str, Any]:
    try:
        text = source.decode("utf-8")
    except UnicodeError as exc:
        raise CatalogBootstrapError(f"{name} is not valid UTF-8: {exc}") from exc

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CatalogBootstrapError(f"{name} contains duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise CatalogBootstrapError(f"{name} contains non-finite number {value}")

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
    except json.JSONDecodeError as exc:
        raise CatalogBootstrapError(f"Invalid {name} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogBootstrapError(f"{name} must be a JSON object")
    return value


def _is_unsafe_link(path: Path) -> bool:
    """Reject symbolic links and Windows reparse points on every supported Python."""

    try:
        metadata = path.lstat()
    except OSError:
        return True
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(file_attributes & reparse_point)


def _bundle_member_bytes(root: Path, relative_path: str, *, maximum_bytes: int) -> bytes:
    candidate = root.joinpath(*relative_path.split("/"))
    if not candidate.is_file() or _is_unsafe_link(candidate):
        raise CatalogBootstrapError(f"Catalog publication bundle member is missing or unsafe: {relative_path}")
    try:
        size = candidate.stat().st_size
        if size < 1 or size > maximum_bytes:
            raise CatalogBootstrapError(
                f"Catalog publication bundle member {relative_path} must be between 1 and {maximum_bytes} bytes"
            )
        return candidate.read_bytes()
    except OSError as exc:
        raise CatalogBootstrapError(f"Could not read catalog publication bundle member {relative_path}: {exc}") from exc


def load_catalog_publication_bundle(
    path: Path | str,
    *,
    now: float | None = None,
) -> Dict[str, Any]:
    """Load and fully validate a deterministic catalog publication bundle directory."""

    root = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    if not root.is_dir() or _is_unsafe_link(root):
        raise CatalogBootstrapError(f"Catalog publication bundle is missing or unsafe: {root}")

    index_bytes = _bundle_member_bytes(
        root,
        CATALOG_PUBLICATION_BUNDLE_INDEX_NAME,
        maximum_bytes=MAX_PUBLICATION_PREFLIGHT_BYTES,
    )
    index = _load_strict_json_document(index_bytes, name="catalog publication bundle index")
    required_index_fields = {
        "schema_version",
        "scope",
        "catalog_id",
        "catalog_sequence",
        "catalog_digest",
        "bootstrap_digest",
        "complete_release_qualification",
        "files",
    }
    if set(index) != required_index_fields:
        missing = sorted(required_index_fields - set(index))
        extra = sorted(set(index) - required_index_fields)
        raise CatalogBootstrapError(
            f"Catalog publication bundle index fields do not match schema; missing={missing}, extra={extra}"
        )
    schema_version = index["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CATALOG_PUBLICATION_BUNDLE_SCHEMA_VERSION
    ):
        raise CatalogBootstrapError(
            f"Unsupported catalog publication bundle schema version {schema_version!r}; "
            f"expected {CATALOG_PUBLICATION_BUNDLE_SCHEMA_VERSION}"
        )
    if index["scope"] != "catalog-publication-bundle":
        raise CatalogBootstrapError("Catalog publication bundle index has an unexpected scope")
    if index["complete_release_qualification"] is not False:
        raise CatalogBootstrapError(
            "Catalog publication bundle must explicitly retain incomplete release qualification"
        )
    if not isinstance(index["files"], list) or not index["files"]:
        raise CatalogBootstrapError("Catalog publication bundle files must be a non-empty array")

    entries: Dict[str, Dict[str, Any]] = {}
    ordered_paths = []
    for position, entry in enumerate(index["files"]):
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise CatalogBootstrapError(
                f"Catalog publication bundle files[{position}] must contain only path, sha256, and size"
            )
        relative_path = entry["path"]
        if not isinstance(relative_path, str):
            raise CatalogBootstrapError(f"Catalog publication bundle files[{position}].path must be a string")
        if relative_path in entries:
            raise CatalogBootstrapError(f"Catalog publication bundle contains duplicate member {relative_path!r}")
        if relative_path not in _FIXED_BUNDLE_MEMBER_PATHS and _MANIFEST_BUNDLE_PATH.fullmatch(relative_path) is None:
            raise CatalogBootstrapError(
                f"Catalog publication bundle contains unsafe or unknown member {relative_path!r}"
            )
        digest = entry["sha256"]
        if not isinstance(digest, str) or _SHA256_DIGEST.fullmatch(digest) is None:
            raise CatalogBootstrapError(
                f"Catalog publication bundle files[{position}].sha256 must be a canonical SHA-256 digest"
            )
        size = entry["size"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or size > MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES
        ):
            raise CatalogBootstrapError(
                f"Catalog publication bundle files[{position}].size must be a bounded positive integer"
            )
        entries[relative_path] = entry
        ordered_paths.append(relative_path)

    if ordered_paths != sorted(ordered_paths):
        raise CatalogBootstrapError("Catalog publication bundle file entries must be ordered by path")
    if len(index_bytes) + sum(entry["size"] for entry in entries.values()) > MAX_CATALOG_PUBLICATION_BUNDLE_TOTAL_BYTES:
        raise CatalogBootstrapError("Catalog publication bundle exceeds the total size limit")
    if not _FIXED_BUNDLE_MEMBER_PATHS.issubset(entries):
        missing = sorted(_FIXED_BUNDLE_MEMBER_PATHS - set(entries))
        raise CatalogBootstrapError(f"Catalog publication bundle is missing fixed member(s): {missing}")

    actual_files = set()
    actual_directories = set()
    try:
        for candidate in root.rglob("*"):
            relative_path = candidate.relative_to(root).as_posix()
            if _is_unsafe_link(candidate):
                raise CatalogBootstrapError(
                    f"Catalog publication bundle contains unsafe symbolic link {relative_path!r}"
                )
            if candidate.is_dir():
                actual_directories.add(relative_path)
            elif candidate.is_file():
                actual_files.add(relative_path)
            else:
                raise CatalogBootstrapError(f"Catalog publication bundle contains unsupported member {relative_path!r}")
    except OSError as exc:
        raise CatalogBootstrapError(f"Could not inspect catalog publication bundle {root}: {exc}") from exc

    expected_files = {CATALOG_PUBLICATION_BUNDLE_INDEX_NAME, *entries}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise CatalogBootstrapError(
            f"Catalog publication bundle members do not match its index; missing={missing}, extra={extra}"
        )
    if actual_directories != {"manifests"}:
        raise CatalogBootstrapError(
            f"Catalog publication bundle directories must contain only manifests; found={sorted(actual_directories)}"
        )

    member_bytes: Dict[str, bytes] = {}
    for relative_path, entry in entries.items():
        raw = _bundle_member_bytes(
            root,
            relative_path,
            maximum_bytes=MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES,
        )
        if len(raw) != entry["size"]:
            raise CatalogBootstrapError(f"Catalog publication bundle member size mismatch for {relative_path}")
        if _sha256_digest_bytes(raw) != entry["sha256"]:
            raise CatalogBootstrapError(f"Catalog publication bundle member digest mismatch for {relative_path}")
        member_bytes[relative_path] = raw

    if index_bytes != _canonical_json_bytes(index):
        raise CatalogBootstrapError("Catalog publication bundle index is not canonical JSON")

    try:
        bootstrap = CatalogBootstrapConfig.from_json(member_bytes["catalog-bootstrap.json"].decode("utf-8"))
        envelope = SignedModelCatalog.from_json(member_bytes["catalog.signed.json"].decode("utf-8"))
        manifest_paths = sorted(set(entries) - _FIXED_BUNDLE_MEMBER_PATHS)
        manifests = tuple(
            ModelManifest.from_json(member_bytes[manifest_path].decode("utf-8")) for manifest_path in manifest_paths
        )
    except (UnicodeError, ModelCatalogError, ManifestError, CatalogBootstrapError) as exc:
        raise CatalogBootstrapError(f"Catalog publication bundle contains an invalid release document: {exc}") from exc

    if member_bytes["catalog-bootstrap.json"] != _canonical_json_bytes(bootstrap.to_dict()):
        raise CatalogBootstrapError("Catalog publication bundle bootstrap is not canonical JSON")
    if member_bytes["catalog.signed.json"] != _canonical_json_bytes(envelope.to_dict()):
        raise CatalogBootstrapError("Catalog publication bundle signed catalog is not canonical JSON")
    expected_manifest_paths = {f"manifests/{manifest.digest}.json" for manifest in manifests}
    if set(manifest_paths) != expected_manifest_paths:
        raise CatalogBootstrapError(
            "Catalog publication bundle manifest filenames do not match their canonical digests"
        )
    for manifest_path, manifest in zip(manifest_paths, manifests):
        expected_manifest_bytes = (manifest.canonical_json() + "\n").encode("utf-8")
        if member_bytes[manifest_path] != expected_manifest_bytes:
            raise CatalogBootstrapError(f"Catalog publication bundle manifest is not canonical JSON: {manifest_path}")

    report = _load_strict_json_document(
        member_bytes["publication-preflight.json"],
        name="catalog publication preflight report",
    )
    if member_bytes["publication-preflight.json"] != _canonical_json_bytes(report):
        raise CatalogBootstrapError("Catalog publication bundle preflight report is not canonical JSON")
    expected_report = verify_catalog_publication_bundle(bootstrap, envelope, manifests, now=now)
    if report != expected_report:
        raise CatalogBootstrapError(
            "Catalog publication bundle preflight report does not match its exact release inputs"
        )
    verify_catalog_publication_preflight_report(bootstrap, report)

    expected_identity = {
        "catalog_id": expected_report["catalog_id"],
        "catalog_sequence": expected_report["catalog_sequence"],
        "catalog_digest": expected_report["catalog_digest"],
        "bootstrap_digest": expected_report["bootstrap_digest"],
    }
    for field, expected in expected_identity.items():
        if index[field] != expected:
            raise CatalogBootstrapError(
                f"Catalog publication bundle index {field} does not match its exact release inputs"
            )
    return index


def write_catalog_publication_bundle(
    output_directory: Path | str,
    bootstrap: CatalogBootstrapConfig,
    envelope: SignedModelCatalog,
    manifests: Sequence[ModelManifest],
    *,
    now: float | None = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Atomically create a canonical publication bundle and validate it before release."""

    report = verify_catalog_publication_bundle(bootstrap, envelope, manifests, now=now)
    ordered_manifests = tuple(sorted(manifests, key=lambda manifest: manifest.digest))
    members: Dict[str, bytes] = {
        "catalog-bootstrap.json": _canonical_json_bytes(bootstrap.to_dict()),
        "catalog.signed.json": _canonical_json_bytes(envelope.to_dict()),
        "publication-preflight.json": _canonical_json_bytes(report),
    }
    for manifest in ordered_manifests:
        relative_path = f"manifests/{manifest.digest}.json"
        if relative_path in members:
            raise CatalogBootstrapError(f"Catalog publication bundle contains duplicate manifest {manifest.digest_id}")
        members[relative_path] = (manifest.canonical_json() + "\n").encode("utf-8")

    files = [
        {
            "path": relative_path,
            "sha256": _sha256_digest_bytes(raw),
            "size": len(raw),
        }
        for relative_path, raw in sorted(members.items())
    ]
    index = {
        "schema_version": CATALOG_PUBLICATION_BUNDLE_SCHEMA_VERSION,
        "scope": "catalog-publication-bundle",
        "catalog_id": report["catalog_id"],
        "catalog_sequence": report["catalog_sequence"],
        "catalog_digest": report["catalog_digest"],
        "bootstrap_digest": report["bootstrap_digest"],
        "complete_release_qualification": False,
        "files": files,
    }

    resolved = Path(os.path.abspath(os.fspath(Path(output_directory).expanduser())))
    if resolved.parent == resolved:
        raise CatalogBootstrapError("Refusing to use a filesystem root as a catalog publication bundle")
    if resolved.exists():
        if not force:
            raise CatalogBootstrapError(
                f"Refusing to overwrite catalog publication bundle {resolved}; pass --force if intentional"
            )
        if not resolved.is_dir() or _is_unsafe_link(resolved):
            raise CatalogBootstrapError(f"Existing catalog publication bundle target is unsafe: {resolved}")
        # --force replaces only a previously valid bundle. This prevents an output
        # typo from recursively deleting an unrelated directory.
        load_catalog_publication_bundle(resolved, now=now)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CatalogBootstrapError(
            f"Could not create catalog publication bundle parent {resolved.parent}: {exc}"
        ) from exc

    token = secrets.token_hex(6)
    staging = resolved.parent / f".{resolved.name}.{os.getpid()}.{token}.tmp"
    backup = resolved.parent / f".{resolved.name}.{os.getpid()}.{token}.bak"
    try:
        staging.mkdir()
        for relative_path, raw in {
            **members,
            CATALOG_PUBLICATION_BUNDLE_INDEX_NAME: _canonical_json_bytes(index),
        }.items():
            destination = staging.joinpath(*relative_path.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(raw)
        verified_index = load_catalog_publication_bundle(staging, now=now)
        if verified_index != index:  # pragma: no cover - loader already enforces exact equality
            raise CatalogBootstrapError("Catalog publication bundle validation returned an unexpected index")

        if resolved.exists():
            os.replace(resolved, backup)
        try:
            os.replace(staging, resolved)
        except OSError:
            if backup.exists() and not resolved.exists():
                os.replace(backup, resolved)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except CatalogBootstrapError:
        raise
    except OSError as exc:
        raise CatalogBootstrapError(f"Could not write catalog publication bundle {resolved}: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return index
