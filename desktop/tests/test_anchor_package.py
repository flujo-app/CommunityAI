"""Real local wheel/sdist roundtrip; no downloads, dependency install or release."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path


def build(root, hook, destination):
    result = subprocess.run(
        [sys.executable, "-c", f"from setuptools.build_meta import {hook}; {hook}({str(destination)!r})"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def build_verified_desktop_wheels(tmp_path):
    desktop = Path(__file__).resolve().parents[1]
    project = tmp_path / "checkout" / "desktop"
    project.mkdir(parents=True)
    for name in ("setup.py", "pyproject.toml", "README.md"):
        shutil.copyfile(desktop / name, project / name)
    shutil.copytree(desktop / "src/communityai_desktop", project / "src/communityai_desktop")
    shared = desktop.parent / "src/communityai_anchor"
    shutil.copytree(shared, project.parent / "src/communityai_anchor", ignore=shutil.ignore_patterns("__pycache__"))
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in shared.glob("*.py")}

    def verify_wheel(wheel):
        with zipfile.ZipFile(wheel) as archive:
            actual = {
                Path(name).name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist()
                if name.startswith("communityai_anchor/") and name.endswith(".py")
            }
            assert actual == expected
            assert not any(name.startswith("drift/") for name in archive.namelist())
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(desktop / "tests/anchor_import_probe.py"), json.dumps([str(wheel)])],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["forbidden_attempts"] == []

    build(project, "build_wheel", tmp_path / "first")
    verify_wheel(next((tmp_path / "first").glob("*.whl")))
    build(project, "build_sdist", tmp_path / "sdist")
    unpacked = tmp_path / "unpacked"
    with tarfile.open(next((tmp_path / "sdist").glob("*.tar.gz"))) as archive:
        for member in archive.getmembers():
            assert not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            assert member.isfile() or member.isdir()
            target = unpacked / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    standalone = next(unpacked.iterdir())
    assert not (standalone.parent / "src/communityai_anchor").exists()
    build(standalone, "build_wheel", tmp_path / "rebuilt")
    verify_wheel(next((tmp_path / "rebuilt").glob("*.whl")))
    return [next((tmp_path / name).glob("*.whl")) for name in ("first", "rebuilt")]


def test_shared_package_survives_desktop_wheel_and_self_contained_sdist(tmp_path):
    assert len(build_verified_desktop_wheels(tmp_path)) == 2


def test_same_checkout_editables_have_one_source_but_wheels_require_isolation(tmp_path):
    desktop_wheel = build_verified_desktop_wheels(tmp_path)[0]
    repository = Path(__file__).resolve().parents[2]
    checkout = tmp_path / "checkout"
    for name in ("setup.py", "pyproject.toml", "README.md"):
        shutil.copyfile(repository / name, checkout / name)
    shutil.copytree(repository / "src/drift", checkout / "src/drift", ignore=shutil.ignore_patterns("__pycache__"))
    build(checkout, "build_wheel", tmp_path / "node-wheel")

    def owned(wheel):
        with zipfile.ZipFile(wheel) as archive:
            record = archive.read(
                next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
            ).decode()
            names = [
                name for name in archive.namelist() if name.startswith("communityai_anchor/") and name.endswith(".py")
            ]
            assert names and all(name in record for name in names)
            return {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names}

    # This deliberately proves overlapping ownership, NOT safe co-installation.
    assert owned(desktop_wheel) == owned(next((tmp_path / "node-wheel").glob("*.whl")))
    environment = tmp_path / "editable-env"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Reuse only existing local tooling/dependencies; no index or dependency
    # installation. The actual editable distributions are installed into the
    # disposable venv, not into the test runner or user's environment.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(python),
            "install",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "-e",
            str(checkout),
            "-e",
            str(checkout / "desktop"),
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    code = """
import json, pathlib, sys
import communityai_anchor
expected = pathlib.Path(sys.argv[1]).resolve()
assert pathlib.Path(communityai_anchor.__file__).resolve() == expected / 'src/communityai_anchor/__init__.py'
# Existing read-only dependencies are appended after the newly installed
# editable checkout. Exercise the real runtime import, not a stub package.
sys.path.extend(json.loads(sys.argv[2]))
from communityai_anchor import linux_anchor_entry as shared
from drift.node import linux_anchor_entry as runtime
assert shared is runtime
assert pathlib.Path(runtime.__file__).resolve() == expected / 'src/communityai_anchor/linux_anchor_entry.py'
print('same-checkout editable identity verified')
"""
    result = subprocess.run(
        [str(python), "-I", "-c", code, str(checkout), json.dumps(sys.path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "same-checkout editable identity verified" in result.stdout
