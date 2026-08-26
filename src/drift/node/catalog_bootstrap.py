"""Install a verified model catalog as a first-run local-node configuration.

The desktop deliberately does not parse catalogs or model manifests.  It invokes
this module inside the standalone node runtime, where the catalog trust root and
manifest implementation already live.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from hivemind.p2p import PeerID

from drift.model_catalog import (
    CatalogRollbackGuard,
    CatalogTrustRoot,
    ModelCatalog,
    ModelCatalogError,
    SignedModelCatalog,
)
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.config import NODE_CONFIG_SCHEMA_VERSION, NodeConfig, NodeConfigError
from drift.node.config_lock import NodeConfigWriteLockError, node_config_write_lock

CATALOG_BOOTSTRAP_SCHEMA_VERSION = 1
MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
DEFAULT_FETCH_TIMEOUT = (5.0, 20.0)
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PUBLIC_PEER_RE = re.compile(r"^/(ip4|ip6|dns4|dns6)/([^/]+)/tcp/([1-9][0-9]{0,4})/p2p/([^/]{20,128})$")
_SPECIAL_USE_DNS_SUFFIXES = (
    ".example",
    ".home",
    ".home.arpa",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
)


class CatalogBootstrapError(RuntimeError):
    """A first-install catalog could not be authenticated and installed."""


def _absolute_path(path: Path | str) -> Path:
    """Make a path absolute without following its final symbolic link."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CatalogBootstrapError(f"{field} must be a JSON object")
    return value


def _require_fields(
    value: Mapping[str, Any], field: str, *, required: Tuple[str, ...], optional: Tuple[str, ...] = ()
) -> None:
    actual = set(value)
    missing = set(required) - actual
    unknown = actual - set(required) - set(optional)
    if missing:
        raise CatalogBootstrapError(f"{field} is missing required field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise CatalogBootstrapError(f"{field} has unknown field(s): {', '.join(sorted(unknown))}")


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CatalogBootstrapError(f"{field} must be an integer >= 1")
    return value


def _require_string_list(value: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CatalogBootstrapError(f"{field} must be a non-empty JSON array")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise CatalogBootstrapError(f"{field}[{index}] must be a non-empty string")
        result.append(item)
    if len(set(result)) != len(result):
        raise CatalogBootstrapError(f"{field} must not contain duplicates")
    return tuple(result)


def _require_public_host(
    host: str,
    field: str,
    *,
    ip_version: int | None = None,
    require_dns: bool = False,
) -> str:
    if (
        not host
        or host.endswith(".")
        or "%" in host
        or any(ord(character) <= 32 or ord(character) == 127 for character in host)
    ):
        raise CatalogBootstrapError(f"{field} must use a canonical public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ip_version is not None:
            raise CatalogBootstrapError(
                f"{field} must use a canonical globally routable IPv{ip_version} address"
            ) from None
        normalized = host.casefold()
        try:
            ascii_host = normalized.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise CatalogBootstrapError(f"{field} must use an ASCII public DNS host") from exc
        labels = ascii_host.split(".")
        if (
            host != normalized
            or ascii_host != normalized
            or len(ascii_host) > 253
            or len(labels) < 2
            or not any("a" <= character <= "z" for character in labels[-1])
            or any(ascii_host == suffix[1:] or ascii_host.endswith(suffix) for suffix in _SPECIAL_USE_DNS_SUFFIXES)
            or any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels)
        ):
            raise CatalogBootstrapError(f"{field} must use a canonical public DNS host")
        return ascii_host
    if require_dns:
        raise CatalogBootstrapError(f"{field} must use a canonical public DNS host")
    if (
        not address.is_global
        or (ip_version is not None and address.version != ip_version)
        or getattr(address, "scope_id", None) is not None
        or str(address) != host
    ):
        raise CatalogBootstrapError(f"{field} must use a canonical globally routable IP address")
    return str(address)


def _require_https_urls(value: Any, field: str) -> Tuple[str, ...]:
    result = _require_string_list(value, field)
    for index, url in enumerate(result):
        if any(ord(character) <= 32 or ord(character) == 127 for character in url):
            raise CatalogBootstrapError(f"{field}[{index}] contains whitespace or a control character")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise CatalogBootstrapError(f"{field}[{index}] contains an invalid port") from exc
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise CatalogBootstrapError(
                f"{field}[{index}] must be an absolute public HTTPS URL without credentials, query, or fragment"
            )
        host = _require_public_host(parsed.hostname, f"{field}[{index}]")
        canonical_netloc = f"[{host}]" if ":" in host else host
        if port == 443:
            canonical_netloc += ":443"
        canonical_url = urlunsplit(("https", canonical_netloc, parsed.path, "", ""))
        if url != canonical_url:
            raise CatalogBootstrapError(f"{field}[{index}] must use a canonical public HTTPS URL")
    return result


def _require_initial_peers(value: Any) -> Tuple[str, ...]:
    peers = _require_string_list(value, "initial_peers")
    for index, peer in enumerate(peers):
        if len(peer) > 2048 or any(ord(character) <= 32 or ord(character) == 127 for character in peer):
            raise CatalogBootstrapError(f"initial_peers[{index}] must be a bounded public libp2p multiaddress")
        match = _PUBLIC_PEER_RE.fullmatch(peer)
        if match is None:
            raise CatalogBootstrapError(
                f"initial_peers[{index}] must be a canonical public multiaddress ending in /tcp/<port>/p2p/<peer-id>"
            )
        protocol, host, port_text, peer_id_text = match.groups()
        if int(port_text) > 65535:
            raise CatalogBootstrapError(f"initial_peers[{index}] contains an invalid TCP port")
        if protocol.startswith("ip"):
            _require_public_host(
                host,
                f"initial_peers[{index}]",
                ip_version=int(protocol[-1]),
            )
        else:
            _require_public_host(
                host,
                f"initial_peers[{index}]",
                require_dns=True,
            )
        try:
            peer_id = PeerID.from_base58(peer_id_text)
        except (TypeError, ValueError) as exc:
            raise CatalogBootstrapError(f"initial_peers[{index}] contains an invalid PeerID") from exc
        if peer_id.to_base58() != peer_id_text:
            raise CatalogBootstrapError(f"initial_peers[{index}] contains a noncanonical PeerID")
    return peers


def _load_strict_json(source: str, *, field: str) -> Mapping[str, Any]:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CatalogBootstrapError(f"{field} contains duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise CatalogBootstrapError(f"{field} contains non-finite number {value}")

    try:
        value = json.loads(source, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
    except json.JSONDecodeError as exc:
        raise CatalogBootstrapError(f"Invalid {field} JSON: {exc}") from exc
    return _require_mapping(value, field)


@dataclass(frozen=True)
class CatalogBootstrapConfig:
    """Immutable release inputs used to establish first-install trust and discovery."""

    schema_version: int
    trust_root: CatalogTrustRoot
    catalog_mirrors: Tuple[str, ...]
    initial_peers: Tuple[str, ...]
    max_loaded_models: int = 1

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> "CatalogBootstrapConfig":
        source = _require_mapping(source, "catalog bootstrap config")
        _require_fields(
            source,
            "catalog bootstrap config",
            required=("schema_version", "trust_root", "catalog_mirrors", "initial_peers"),
            optional=("max_loaded_models",),
        )
        schema_version = _require_positive_int(source["schema_version"], "schema_version")
        if schema_version != CATALOG_BOOTSTRAP_SCHEMA_VERSION:
            raise CatalogBootstrapError(
                f"Unsupported catalog bootstrap schema version {schema_version}; "
                f"expected {CATALOG_BOOTSTRAP_SCHEMA_VERSION}"
            )
        try:
            trust_root = CatalogTrustRoot.from_dict(_require_mapping(source["trust_root"], "trust_root"))
        except ModelCatalogError as exc:
            raise CatalogBootstrapError(f"Invalid catalog trust root: {exc}") from exc
        return cls(
            schema_version=schema_version,
            trust_root=trust_root,
            catalog_mirrors=_require_https_urls(source["catalog_mirrors"], "catalog_mirrors"),
            initial_peers=_require_initial_peers(source["initial_peers"]),
            max_loaded_models=_require_positive_int(source.get("max_loaded_models", 1), "max_loaded_models"),
        )

    @classmethod
    def from_json(cls, source: str) -> "CatalogBootstrapConfig":
        return cls.from_dict(_load_strict_json(source, field="catalog bootstrap config"))

    @classmethod
    def load(cls, path: Path | str) -> "CatalogBootstrapConfig":
        resolved = _absolute_path(path)
        if not resolved.is_file() or resolved.is_symlink():
            raise CatalogBootstrapError(f"Catalog bootstrap config is missing or unsafe: {resolved}")
        try:
            source = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CatalogBootstrapError(f"Could not read catalog bootstrap config {resolved}: {exc}") from exc
        return cls.from_json(source)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trust_root": self.trust_root.to_dict(),
            "catalog_mirrors": list(self.catalog_mirrors),
            "initial_peers": list(self.initial_peers),
            "max_loaded_models": self.max_loaded_models,
        }


@dataclass(frozen=True)
class CatalogBootstrapResult:
    config_path: Path
    catalog_id: str
    catalog_sequence: int
    catalog_digest: str
    model_count: int
    source: str
    created: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "config_path": str(self.config_path),
            "catalog_id": self.catalog_id,
            "catalog_sequence": self.catalog_sequence,
            "catalog_digest": self.catalog_digest,
            "model_count": self.model_count,
            "source": self.source,
            "created": self.created,
        }


FetchText = Callable[[str, int], str]


def _fetch_https_text(url: str, maximum_bytes: int) -> str:
    """Fetch one bounded HTTPS document without redirects or environment proxies."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - root runtime always declares requests
        raise CatalogBootstrapError("The node runtime is missing its HTTPS client") from exc

    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            url,
            allow_redirects=False,
            stream=True,
            timeout=DEFAULT_FETCH_TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": "CommunityAI-Node/catalog-bootstrap-v1"},
        )
        if response.status_code != 200:
            raise CatalogBootstrapError(f"HTTPS fetch returned status {response.status_code} for {url}")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise CatalogBootstrapError(f"HTTPS response has an invalid Content-Length for {url}") from exc
            if declared_length < 0 or declared_length > maximum_bytes:
                raise CatalogBootstrapError(f"HTTPS response exceeds the size limit for {url}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > maximum_bytes:
                raise CatalogBootstrapError(f"HTTPS response exceeds the size limit for {url}")
        try:
            return bytes(body).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CatalogBootstrapError(f"HTTPS response is not UTF-8 JSON for {url}") from exc
    except requests.RequestException as exc:
        raise CatalogBootstrapError(f"HTTPS fetch failed for {url}: {exc}") from exc
    finally:
        session.close()


def _atomic_write(path: Path, text: str, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        if not overwrite and (path.exists() or path.is_symlink()):
            raise CatalogBootstrapError(f"Refusing to replace existing first-install file {path}")
        os.replace(temporary, path)
    except CatalogBootstrapError:
        raise
    except OSError as exc:
        raise CatalogBootstrapError(f"Could not write first-install file {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class CatalogBootstrapInstaller:
    """Fetch, authenticate, cache, and atomically activate a first-install catalog."""

    def __init__(
        self,
        bootstrap: CatalogBootstrapConfig,
        *,
        data_dir: Path | str,
        config_path: Path | str,
        fetch_text: FetchText = _fetch_https_text,
        now: Optional[float] = None,
    ) -> None:
        self.bootstrap = bootstrap
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.config_path = _absolute_path(config_path)
        self.fetch_text = fetch_text
        self.now = now
        self.catalog_dir = self.data_dir / "catalogs" / bootstrap.trust_root.catalog_id
        self.cached_catalog_path = self.catalog_dir / "catalog.signed.json"
        self.rollback_path = self.catalog_dir / "rollback-state.json"
        self.manifest_dir = self.data_dir / "manifests"
        self.cache_dir = self.data_dir / "model-cache"
        self.lock_path = self.data_dir / ".catalog-bootstrap.lock"

    def _existing_result(self) -> CatalogBootstrapResult:
        try:
            config = NodeConfig.load(self.config_path)
        except NodeConfigError as exc:
            raise CatalogBootstrapError(f"Existing node configuration is invalid: {exc}") from exc
        return CatalogBootstrapResult(
            config_path=self.config_path,
            catalog_id=self.bootstrap.trust_root.catalog_id,
            catalog_sequence=0,
            catalog_digest="",
            model_count=len(config.models),
            source="existing-config",
            created=False,
        )

    def _load_catalog(self, source: str, rendered: str, guard: CatalogRollbackGuard) -> ModelCatalog:
        try:
            envelope = SignedModelCatalog.from_json(rendered)
            return envelope.verify(self.bootstrap.trust_root, now=self.now, rollback_guard=guard)
        except ModelCatalogError as exc:
            raise CatalogBootstrapError(f"Rejected model catalog from {source}: {exc}") from exc

    def _install_manifests(self, catalog: ModelCatalog) -> Tuple[Path, ...]:
        installed = []
        selectors: Dict[str, str] = {}
        for model in catalog.models:
            errors = []
            manifest = None
            expected_path = self.manifest_dir / f"{model.manifest_digest.removeprefix('sha256:')}.json"
            if expected_path.is_file() and not expected_path.is_symlink():
                try:
                    candidate = ModelManifest.load(expected_path)
                    if candidate.digest_id != model.manifest_digest:
                        raise CatalogBootstrapError(
                            f"cached manifest digest {candidate.digest_id} does not match {model.manifest_digest}"
                        )
                    manifest = candidate
                except (CatalogBootstrapError, ManifestError) as exc:
                    errors.append(str(exc))
            for url in model.manifest_urls:
                if manifest is not None:
                    break
                try:
                    candidate = ModelManifest.from_json(self.fetch_text(url, MAX_MANIFEST_BYTES))
                    if candidate.digest_id != model.manifest_digest:
                        raise CatalogBootstrapError(
                            f"manifest digest {candidate.digest_id} does not match catalog digest {model.manifest_digest}"
                        )
                    manifest = candidate
                    break
                except (CatalogBootstrapError, ManifestError) as exc:
                    errors.append(str(exc))
            if manifest is None:
                detail = "; ".join(errors) if errors else "no manifest mirror was attempted"
                raise CatalogBootstrapError(f"Could not install manifest {model.manifest_digest}: {detail}")

            for selector in (manifest.name, *manifest.aliases):
                folded = selector.casefold()
                previous = selectors.get(folded)
                if previous is not None and previous != manifest.digest_id:
                    raise CatalogBootstrapError(
                        f"Catalog manifests reuse model selector {selector!r} across different digests"
                    )
                selectors[folded] = manifest.digest_id

            path = self.manifest_dir / f"{manifest.digest}.json"
            _atomic_write(path, manifest.canonical_json() + "\n", overwrite=True)
            installed.append(path)
        return tuple(installed)

    def _render_node_config(self, manifest_paths: Tuple[Path, ...]) -> str:
        models = []
        for path in manifest_paths:
            digest = path.stem
            models.append(
                {
                    "manifest": str(path),
                    "initial_peers": list(self.bootstrap.initial_peers),
                    "cache_dir": str(self.cache_dir / digest),
                }
            )
        source = {
            "schema_version": NODE_CONFIG_SCHEMA_VERSION,
            "max_loaded_models": self.bootstrap.max_loaded_models,
            "models": models,
            "workers": [],
        }
        try:
            NodeConfig.from_dict(source, base_dir=self.config_path.parent)
        except NodeConfigError as exc:  # pragma: no cover - defensive invariant
            raise CatalogBootstrapError(f"Generated node configuration is invalid: {exc}") from exc
        return json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _try_candidate(
        self, source: str, rendered: str, persisted_guard: CatalogRollbackGuard
    ) -> CatalogBootstrapResult:
        guard = CatalogRollbackGuard.from_dict(persisted_guard.to_dict())
        catalog = self._load_catalog(source, rendered, guard)
        manifest_paths = self._install_manifests(catalog)
        config_text = self._render_node_config(manifest_paths)

        _atomic_write(
            self.cached_catalog_path,
            json.dumps(SignedModelCatalog.from_json(rendered).to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            overwrite=True,
        )
        guard.save(self.rollback_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with node_config_write_lock(self.config_path):
                _atomic_write(self.config_path, config_text, overwrite=False)
        except NodeConfigWriteLockError as exc:
            raise CatalogBootstrapError("Another node configuration writer is active") from exc
        return CatalogBootstrapResult(
            config_path=self.config_path,
            catalog_id=catalog.catalog_id,
            catalog_sequence=catalog.sequence,
            catalog_digest=catalog.digest,
            model_count=len(manifest_paths),
            source=source,
            created=True,
        )

    def install(self) -> CatalogBootstrapResult:
        if self.config_path.is_symlink():
            raise CatalogBootstrapError(f"Refusing unsafe node configuration symlink {self.config_path}")
        if self.config_path.is_file():
            return self._existing_result()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise CatalogBootstrapError("Another first-install catalog bootstrap is already in progress") from exc
        except OSError as exc:
            raise CatalogBootstrapError(f"Could not lock catalog bootstrap in {self.data_dir}: {exc}") from exc

        os.close(descriptor)
        try:
            if self.config_path.is_file() and not self.config_path.is_symlink():
                return self._existing_result()
            try:
                persisted_guard = CatalogRollbackGuard.load(self.rollback_path)
            except ModelCatalogError as exc:
                raise CatalogBootstrapError(f"Could not load catalog rollback protection: {exc}") from exc

            errors = []
            for url in self.bootstrap.catalog_mirrors:
                try:
                    rendered = self.fetch_text(url, MAX_CATALOG_BYTES)
                    return self._try_candidate(url, rendered, persisted_guard)
                except CatalogBootstrapError as exc:
                    errors.append(str(exc))
                    # _try_candidate persists rollback state before activating the
                    # node config. Reload it before considering another mirror so
                    # an activation I/O failure can never enable a downgrade.
                    try:
                        persisted_guard = CatalogRollbackGuard.load(self.rollback_path)
                    except ModelCatalogError as guard_exc:
                        raise CatalogBootstrapError(
                            f"Could not reload catalog rollback protection: {guard_exc}"
                        ) from guard_exc
            if self.cached_catalog_path.is_file() and not self.cached_catalog_path.is_symlink():
                try:
                    rendered = self.cached_catalog_path.read_text(encoding="utf-8")
                    return self._try_candidate(
                        "last-known-good cache",
                        rendered,
                        persisted_guard,
                    )
                except (OSError, UnicodeError, CatalogBootstrapError) as exc:
                    errors.append(f"Could not use last-known-good catalog: {exc}")
            detail = "; ".join(errors) if errors else "no catalog source was available"
            raise CatalogBootstrapError(f"No trusted usable model catalog could be installed: {detail}")
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass


def bootstrap_node_from_catalog(
    bootstrap_config: Path | str,
    *,
    data_dir: Path | str,
    config_path: Path | str,
    fetch_text: FetchText = _fetch_https_text,
    now: Optional[float] = None,
) -> CatalogBootstrapResult:
    bootstrap = CatalogBootstrapConfig.load(bootstrap_config)
    return CatalogBootstrapInstaller(
        bootstrap,
        data_dir=data_dir,
        config_path=config_path,
        fetch_text=fetch_text,
        now=now,
    ).install()
