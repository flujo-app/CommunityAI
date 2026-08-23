"""Build and smoke-test the unsigned production desktop bundle."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from communityai_desktop.acceptance import run_self_test
from communityai_desktop.pyside_shell import check_runtime

APP_NAME = "CommunityAI"
NODE_NAME = "CommunityAI-Node"
NODE_DIRECTORY = "node"
FORBIDDEN_RUNTIME_PACKAGES = ("drift", "torch", "transformers", "hivemind", "accelerate")


def _directory_metrics(path: Path) -> tuple[int, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return sum(item.stat().st_size for item in files), len(files)


def _run_bundle(
    executable: Path, arguments: str | Sequence[str], environment: dict[str, str], *, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    if isinstance(arguments, str):
        arguments = (arguments,)
    result = subprocess.run(
        [str(executable), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if result.returncode:
        raise RuntimeError(
            f"packaged executable failed {' '.join(arguments)} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout.strip()}\n"
            f"stderr:\n{result.stderr.strip()}"
        )
    return result


def _run_pyinstaller(arguments: list[str]) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", *arguments], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--bootstrap-config", type=Path)
    args = parser.parse_args()

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        parser.error(f"PyInstaller is not installed: {exc}")

    project = Path(__file__).resolve().parent
    repository = project.parent
    output_root = (args.output_root or project / "dist" / "desktop").resolve()
    build_root = project / "build" / "desktop"
    bundle_root = output_root / APP_NAME
    icon_path = project / "src" / "communityai_desktop" / "assets" / "communityai.ico"
    if not icon_path.is_file():
        raise RuntimeError(f"desktop icon is missing: {icon_path}; run generate_assets.py")
    bootstrap_config = args.bootstrap_config or project / "release" / "catalog-bootstrap.json"
    bootstrap_config = bootstrap_config.expanduser().resolve()
    if args.bootstrap_config is not None and not bootstrap_config.is_file():
        raise RuntimeError(f"release bootstrap config is missing: {bootstrap_config}")
    if bootstrap_config.is_file():
        from drift.node.catalog_bootstrap import CatalogBootstrapConfig

        CatalogBootstrapConfig.load(bootstrap_config)

    pyinstaller_args = [
        str(project / "launch_desktop.py"),
        "--name",
        APP_NAME,
        "--onedir",
        "--noconfirm",
        "--clean",
        "--paths",
        str(project / "src"),
        "--distpath",
        str(output_root),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root / "spec"),
        "--hidden-import",
        "communityai_desktop.pyside_shell",
        "--add-data",
        f"{icon_path}{os.pathsep}communityai_desktop/assets",
    ]
    if bootstrap_config.is_file():
        pyinstaller_args.extend(("--add-data", f"{bootstrap_config}{os.pathsep}bootstrap"))
    if platform.system() == "Windows":
        # The product executable is a GUI application. Diagnostic actions still
        # return meaningful exit codes but do not open a console window.
        pyinstaller_args.append("--noconsole")
        pyinstaller_args.extend(("--icon", str(icon_path)))
    for package in FORBIDDEN_RUNTIME_PACKAGES:
        pyinstaller_args.extend(("--exclude-module", package))
    credential_backend = {
        "Windows": "keyring.backends.Windows",
        "Darwin": "keyring.backends.macOS",
        "Linux": "keyring.backends.SecretService",
    }.get(platform.system())
    if credential_backend:
        pyinstaller_args.extend(("--hidden-import", credential_backend))

    _run_pyinstaller(pyinstaller_args)

    executable = bundle_root / f"{APP_NAME}{'.exe' if os.name == 'nt' else ''}"
    if not executable.is_file():
        raise RuntimeError(f"packaged executable was not created: {executable}")

    sidecar_dist = build_root / "sidecar-dist"
    node_args = [
        str(project / "launch_node.py"),
        "--name",
        NODE_NAME,
        "--onedir",
        "--noconfirm",
        "--clean",
        "--paths",
        str(repository / "src"),
        "--distpath",
        str(sidecar_dist),
        "--workpath",
        str(build_root / "sidecar-work"),
        "--specpath",
        str(build_root / "sidecar-spec"),
        "--collect-all",
        "hivemind",
        "--collect-submodules",
        "drift",
        "--exclude-module",
        "PySide6",
    ]
    credential_backend = {
        "Windows": "keyring.backends.Windows",
        "Darwin": "keyring.backends.macOS",
        "Linux": "keyring.backends.SecretService",
    }.get(platform.system())
    if credential_backend:
        node_args.extend(("--hidden-import", credential_backend))
    _run_pyinstaller(node_args)

    built_sidecar = sidecar_dist / NODE_NAME
    node_root = bundle_root / NODE_DIRECTORY
    if not built_sidecar.is_dir():
        raise RuntimeError(f"packaged node directory was not created: {built_sidecar}")
    if node_root.exists():
        shutil.rmtree(node_root)
    # Move rather than duplicate the multi-gigabyte runtime inside the CI workspace.
    shutil.move(str(built_sidecar), str(node_root))
    node_executable = node_root / f"{NODE_NAME}{'.exe' if os.name == 'nt' else ''}"
    if not node_executable.is_file():
        raise RuntimeError(f"packaged node executable was not staged: {node_executable}")

    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    runtime = check_runtime()
    contract = run_self_test()
    _run_bundle(executable, "--check-runtime", environment)
    _run_bundle(executable, "--self-test", environment)
    _run_bundle(executable, "--ui-self-test", environment)
    _run_bundle(executable, "--onboarding-ui-self-test", environment)
    node_contract = json.loads(_run_bundle(node_executable, "--self-test", environment, timeout=180).stdout)
    _run_bundle(node_executable, "--help", environment, timeout=180)
    _run_bundle(node_executable, ("bootstrap", "--help"), environment, timeout=180)
    _run_bundle(node_executable, ("server", "--help"), environment, timeout=180)
    bundle_bytes, file_count = _directory_metrics(bundle_root)
    node_bytes, node_file_count = _directory_metrics(node_root)
    metrics = {
        "schema_version": 1,
        "application": APP_NAME,
        "package": "communityai-desktop",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "bundle_bytes": bundle_bytes,
        "file_count": file_count,
        "runtime": runtime,
        "acceptance": contract,
        "ui_smoke_passed": True,
        "onboarding_ui_smoke_passed": True,
        "node_sidecar": {
            "relative_executable": str(node_executable.relative_to(bundle_root)),
            "bundle_bytes": node_bytes,
            "file_count": node_file_count,
            "runtime": node_contract,
            "self_test_passed": True,
            "node_entrypoint_smoke_passed": True,
            "worker_entrypoint_smoke_passed": True,
        },
        "console_window": platform.system() != "Windows",
        "signed": False,
        "catalog_bootstrap_bundled": bootstrap_config.is_file(),
    }
    metrics_path = output_root / "desktop-metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
