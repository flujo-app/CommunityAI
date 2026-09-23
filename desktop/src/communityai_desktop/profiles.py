"""Fixed desktop profile identities for side-by-side engineering builds.

This separates application state; it is not a sandbox against the same OS user.
The volunteer profile never imports or rebases the regular application's files.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

VOLUNTEER_PROFILE = "multigpu-volunteer"
_MAX_CONFIG_BYTES = 256 * 1024


def _private_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError("The test profile cannot use symbolic links or directory junctions")
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("The test profile cannot use redirected paths")
        if candidate == path and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("The test profile cannot use shared hard-linked files")
    return path


@dataclass(frozen=True)
class VolunteerProfile:
    root: Path
    name: str = VOLUNTEER_PROFILE
    application_name: str = "CommunityAI Multi-GPU Test"
    node_url: str = "http://127.0.0.1:18081"
    credential_service: str = "org.communityai.desktop.multigpu-volunteer"
    credential_account: str = "multigpu-volunteer-control-v1"

    @classmethod
    def for_current_user(cls) -> "VolunteerProfile":
        return cls(Path.home() / ".communityai" / VOLUNTEER_PROFILE)

    @property
    def data_dir(self) -> Path:
        return self.root / "node"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "node-config.json"

    @property
    def instance_dir(self) -> Path:
        return self.root / "desktop"

    def child_environment(self) -> dict[str, str | None]:
        cache = self.root / "cache"
        paths = {
            "DRIFT_CACHE": cache / "drift",
            "HF_HOME": cache / "huggingface",
            "HF_HUB_CACHE": cache / "huggingface" / "hub",
            "HUGGINGFACE_HUB_CACHE": cache / "huggingface" / "hub",
            "TRANSFORMERS_CACHE": cache / "huggingface" / "hub",
            "HF_ASSETS_CACHE": cache / "huggingface" / "assets",
            "HUGGINGFACE_ASSETS_CACHE": cache / "huggingface" / "assets",
            "HF_XET_CACHE": cache / "huggingface" / "xet",
            "HF_TOKEN_PATH": cache / "huggingface" / "token",
            "TORCH_HOME": cache / "torch",
            "TORCH_EXTENSIONS_DIR": cache / "torch-extensions",
            "TORCHINDUCTOR_CACHE_DIR": cache / "torch-inductor",
            "TRITON_CACHE_DIR": cache / "triton",
            "CUDA_CACHE_PATH": cache / "cuda",
            "XDG_CACHE_HOME": cache / "xdg",
            "TMPDIR": self.root / "tmp",
            "TEMP": self.root / "tmp",
            "TMP": self.root / "tmp",
        }
        return {
            **{key: str(_private_path(path)) for key, path in paths.items()},
            "HF_TOKEN": None,
            "HUGGING_FACE_HUB_TOKEN": None,
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        }

    def prepare(self, *, existing_anchor=False) -> None:
        """Check known state before keyring access, then create private directories."""
        root = _private_path(self.root)
        if not self.root.is_absolute() or root == Path(root.anchor):
            raise ValueError("The test profile requires its own absolute directory")
        paths = (root, self.data_dir, self.instance_dir, root / "cache", root / "tmp")
        self.child_environment()  # Validate inherited-cache replacements before creating anything.
        self.validate_state_paths()
        self.validate_config()
        for path in paths:
            if existing_anchor and path in (root, self.data_dir):
                if not _private_path(path).is_dir():
                    raise ValueError("The provisioned test profile is missing; checked recovery is required")
            else:
                _private_path(path).mkdir(mode=0o700, parents=not existing_anchor, exist_ok=True)
            if os.name != "nt":
                info = path.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("The test profile requires private directories owned by the current user")

    def _contained(self, value: object, field: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"The test profile has an invalid {field}")
        path = Path(value).expanduser()
        if ".." in path.parts:
            raise ValueError(f"The test profile's {field} may traverse outside its own storage")
        if not path.is_absolute():
            path = self.config_path.parent / path
        path = _private_path(path)
        try:
            path.relative_to(_private_path(self.root))
        except ValueError:
            raise ValueError(f"The test profile's {field} points outside its own storage") from None

    def validate_config(self) -> None:
        """Reject imported/shared paths before bootstrap migration and node startup."""
        self.validate_state_paths()
        path = _private_path(self.config_path)
        if not path.exists():
            return
        if not path.is_file() or path.stat().st_size > _MAX_CONFIG_BYTES:
            raise ValueError("The test profile configuration is invalid or too large")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("The test profile configuration has duplicate fields")
                result[key] = value
            return result

        try:
            with path.open("rb") as stream:
                payload = stream.read(_MAX_CONFIG_BYTES + 1)
            if len(payload) > _MAX_CONFIG_BYTES:
                raise ValueError("The test profile configuration is too large")
            document = json.loads(payload, object_pairs_hook=unique)
            if not isinstance(document, dict):
                raise ValueError("The test profile configuration must be an object")
            for field in ("catalog_path", "catalog_bootstrap_path"):
                if document.get(field) is not None:
                    self._contained(document[field], field)
            for group, fields in (("models", ("manifest", "cache_dir")), ("workers", ("identity_path", "cache_dir"))):
                entries = document.get(group, [])
                if not isinstance(entries, list):
                    raise ValueError(f"The test profile's {group} must be a list")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError(f"The test profile's {group} contains an invalid entry")
                    for field in fields:
                        if entry.get(field) is not None:
                            self._contained(entry[field], f"{group}.{field}")
                    if group == "models":
                        revocations = entry.get("revocation_files", [])
                        if not isinstance(revocations, list):
                            raise ValueError("The test profile has invalid revocation paths")
                        for revocation in revocations:
                            self._contained(revocation, "models.revocation_files")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("The test profile configuration could not be read safely") from exc

    def validate_state_paths(self) -> None:
        self.child_environment()
        for name in (
            "control-api.key",
            "local-api.key",
            "api-keys.json",
            "discovery-peers.json",
            "route-demand.key",
            "replay-history",
            "worker-identities",
            "model-cache",
            "manifests",
            "catalogs",
        ):
            _private_path(self.data_dir / name)
