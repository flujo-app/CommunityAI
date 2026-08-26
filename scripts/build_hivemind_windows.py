"""
Build a patched hivemind wheel for Windows.

Usage (from repo root in a PowerShell or cmd with Go on PATH):
    python scripts/build_hivemind_windows.py [--out-dir DIST_DIR]

What it does:
1.  Downloads the hivemind 1.1.12 sdist from PyPI into a temp directory.
2.  Applies scripts/hivemind-win32.patch (relative to this script's parent directory).
3.  Builds the Go p2pd.exe binary (requires `go` on PATH, Go >= 1.13).
4.  Builds a wheel with `pip wheel .` (requires `pip` on PATH, or the venv-activated pip).
5.  Copies the wheel to DIST_DIR (default: dist/).

The resulting wheel satisfies `hivemind>=1.1.12,<1.2` and can be installed before
`pip install -e .` on Windows (the pyproject.toml hivemind dep is gated
`; sys_platform != 'win32'`, so pip/uv won't pull in the PyPI copy).

Requirements on the build machine:
  - Python 3.10+ (matching target venv)
  - Go >= 1.13 on PATH
  - pip (from any venv is fine)
  - patch utility, Git, OR the Python `patch` package
    (this script tries those patch engines in that order)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

SDIST_URL = (
    "https://files.pythonhosted.org/packages/60/41/"
    "1aac9b64f9aa174dd8ff03a997df52c9652e9326b376f1f4eaa066e6a358/"
    "hivemind-1.1.12.tar.gz"
)
SDIST_NAME = "hivemind-1.1.12.tar.gz"
SDIST_DIR = "hivemind-1.1.12"

HERE = Path(__file__).parent.resolve()
PATCH_FILE = HERE / "hivemind-win32.patch"


def run(cmd, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, check=True, **kwargs)
    return result


def ensure_pip() -> None:
    """Make sure `sys.executable -m pip` works, including in uv venvs created without pip."""
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if probe.returncode == 0:
        return
    run([sys.executable, "-m", "ensurepip", "--upgrade"])


def apply_patch(patch_file: Path, target_dir: Path) -> None:
    """Apply a unified diff patch. Tries system `patch`, Git, then a pure-Python fallback."""
    # Feed raw bytes: text=True would re-encode through the locale codepage on Windows
    # (e.g. cp1252), silently corrupting any non-ASCII byte in the patched files.
    patch_bytes = patch_file.read_bytes().replace(b"\r\n", b"\n")
    # Try system patch utility
    patch_cmd = shutil.which("patch")
    if patch_cmd:
        try:
            subprocess.run(
                [patch_cmd, "-p1", "-d", str(target_dir)],
                input=patch_bytes,
                check=True,
            )
            return
        except subprocess.CalledProcessError as e:
            print(f"Warning: system patch failed ({e}), trying Git fallback")

    git_cmd = shutil.which("git")
    if git_cmd:
        try:
            subprocess.run(
                [git_cmd, "apply", "--whitespace=nowarn", "-"],
                cwd=target_dir,
                input=patch_bytes,
                check=True,
            )
            return
        except subprocess.CalledProcessError as e:
            print(f"Warning: Git patch failed ({e}), trying pure-Python fallback")

    # Pure-Python fallback using the `patch` PyPI package
    try:
        import patch as patch_module  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "Could not find a `patch` utility. Either install the system `patch` command "
            "or run `pip install patch` to get a pure-Python fallback."
        )
    pset = patch_module.fromstring(patch_bytes)
    if not pset.apply(root=str(target_dir)):
        raise RuntimeError("patch.apply() failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build patched hivemind wheel for Windows")
    parser.add_argument("--out-dir", default="dist", help="Output directory for the wheel (default: dist/)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not PATCH_FILE.exists():
        print(f"Error: patch file not found at {PATCH_FILE}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="hivemind-win-build-") as tmpdir:
        tmp = Path(tmpdir)

        # 1. Download sdist
        sdist_path = tmp / SDIST_NAME
        print(f"Downloading {SDIST_URL} ...")
        urllib.request.urlretrieve(SDIST_URL, sdist_path)

        # 2. Extract
        print("Extracting sdist ...")
        with tarfile.open(sdist_path, "r:gz") as tar:
            tar.extractall(tmp)

        src_dir = tmp / SDIST_DIR
        if not src_dir.exists():
            raise RuntimeError(f"Expected extracted dir {src_dir}")

        # 3. Apply patch
        print("Applying patch ...")
        apply_patch(PATCH_FILE, src_dir)

        # 4. Build wheel (HIVEMIND_BUILDGO=1 triggers go build in setup.py)
        print("Building wheel (this will compile p2pd.exe via Go) ...")
        env = os.environ.copy()
        env["HIVEMIND_BUILDGO"] = "1"

        # Ensure grpcio-tools is available for proto compilation
        ensure_pip()
        run([sys.executable, "-m", "pip", "install", "--quiet", "grpcio-tools>=1.33.2"])

        run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(out_dir), str(src_dir)],
            env=env,
        )

    wheels = sorted(out_dir.glob("hivemind-*.whl"))
    if not wheels:
        print("Error: no wheel produced", file=sys.stderr)
        sys.exit(1)

    print(f"\nSuccess! Wheel built: {wheels[-1]}")
    print(f"\nInstall with:")
    print(f'  pip install "{wheels[-1]}"')
    print("Then install drift (hivemind excluded by sys_platform marker):")
    print("  pip install -e .")


if __name__ == "__main__":
    main()
