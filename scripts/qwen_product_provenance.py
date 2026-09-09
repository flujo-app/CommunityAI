"""Capture the replay's actual source bytes and package inputs before cloud mutation."""

import hashlib
import io
import json
import subprocess
import tarfile
import time
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def source_files(root):
    # Deliberate source-only roots: never include keys, caches, logs or build trees.
    names = {"pyproject.toml", "README.md", "LICENSE", "desktop/pyproject.toml"}
    names.update(p.name for p in root.glob("Run Qwen*.cmd"))
    for directory, suffixes in (
        ("scripts", {".py", ".ps1"}),
        ("src/drift", None),
        ("desktop/src", None),
        ("manifests/candidates", {".json"}),
        ("public-alpha/catalog-qwen-v2", {".json"}),
        ("config", {".json"}),
    ):
        names.update(
            p.relative_to(root).as_posix()
            for p in (root / directory).rglob("*")
            if p.is_file()
            and (suffixes is None or p.suffix in suffixes)
            and p.suffix not in {".pyc", ".pyo"}
            and "__pycache__" not in p.parts
        )
    return sorted(names)


def snapshot(root, run, input_paths):
    files = {}
    archive_path = run / "launcher-source.tar.gz"
    with tarfile.open(archive_path, "x:gz") as archive:
        for name in source_files(root):
            payload = (root / name).read_bytes()
            files[name] = hashlib.sha256(payload).hexdigest()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=30)
    value = {
        "schema_version": 1,
        "run_id": run.name,
        "recorded_at_unix": time.time(),
        "checkout_head": git.stdout.strip(),
        "source_identity": "archived working-tree bytes; checkout HEAD is not a package build claim",
        "files": files,
        "archive_sha256": sha256(archive_path),
        "inputs": {label: {"path": str(p.resolve()), "sha256": sha256(p)} for label, p in input_paths.items()},
    }
    (run / "launcher-source.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return value


def verify_snapshot(root, run, value):
    changed = sorted(set(source_files(root)) ^ set(value["files"]))
    changed += [
        name for name, digest in value["files"].items() if not (root / name).is_file() or sha256(root / name) != digest
    ]
    changed += [
        "input:" + label
        for label, binding in value["inputs"].items()
        if not Path(binding["path"]).is_file() or sha256(binding["path"]) != binding["sha256"]
    ]
    if sha256(run / "launcher-source.tar.gz") != value["archive_sha256"]:
        changed.append("launcher-source.tar.gz")
    if changed:
        raise ValueError("Replay inputs changed during execution: " + ", ".join(changed))


def verify_package(node, provenance_path, expected_sha256):
    """Verify the complete existing package, including DLLs, not only its small entrypoint."""
    if len(expected_sha256) != 64 or sha256(node) != expected_sha256:
        raise ValueError("Packaged node SHA-256 does not match the configured build")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    base = provenance_path.parent.resolve()
    observed = set()
    for item in provenance["artifacts"]:
        path = (base / item["path"]).resolve()
        if not path.is_relative_to(base) or item["kind"] != "file":
            raise ValueError("This Windows replay requires regular package files within its build directory")
        if not path.is_file() or path.stat().st_size != item["size_bytes"] or sha256(path) != item["sha256"]:
            raise ValueError("Package provenance mismatch: " + item["path"])
        observed.add(path)
    if node.resolve() not in observed:
        raise ValueError("Package provenance does not bind the selected node")
    artifact_root = (base / provenance.get("artifact_root", "CommunityAI")).resolve()
    if not artifact_root.is_relative_to(base):
        raise ValueError("Package root escapes its build directory")
    actual = {p.resolve() for p in artifact_root.rglob("*") if p.is_file()}
    if actual != observed:
        raise ValueError("Package contains missing or unlisted runtime files")
    return {"verified_files": len(observed), "node_sha256": expected_sha256}
