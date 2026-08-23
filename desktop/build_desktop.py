"""Build and smoke-test the unsigned production desktop bundle."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from pathlib import Path

from communityai_desktop.acceptance import run_self_test
from communityai_desktop.pyside_shell import check_runtime

APP_NAME = "CommunityAI"
FORBIDDEN_RUNTIME_PACKAGES = ("drift", "torch", "transformers", "hivemind", "accelerate")


def _directory_metrics(path: Path) -> tuple[int, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return sum(item.stat().st_size for item in files), len(files)


def _run_bundle(executable: Path, action: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(executable), action],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if result.returncode:
        raise RuntimeError(
            f"packaged executable failed {action} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout.strip()}\n"
            f"stderr:\n{result.stderr.strip()}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        parser.error(f"PyInstaller is not installed: {exc}")

    project = Path(__file__).resolve().parent
    output_root = (args.output_root or project / "dist" / "desktop").resolve()
    build_root = project / "build" / "desktop"
    bundle_root = output_root / APP_NAME
    icon_path = project / "src" / "communityai_desktop" / "assets" / "communityai.ico"
    if not icon_path.is_file():
        raise RuntimeError(f"desktop icon is missing: {icon_path}; run generate_assets.py")

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

    PyInstaller.__main__.run(pyinstaller_args)

    executable = bundle_root / f"{APP_NAME}{'.exe' if os.name == 'nt' else ''}"
    if not executable.is_file():
        raise RuntimeError(f"packaged executable was not created: {executable}")
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    runtime = check_runtime()
    contract = run_self_test()
    _run_bundle(executable, "--check-runtime", environment)
    _run_bundle(executable, "--self-test", environment)
    _run_bundle(executable, "--ui-self-test", environment)
    _run_bundle(executable, "--onboarding-ui-self-test", environment)
    bundle_bytes, file_count = _directory_metrics(bundle_root)
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
        "console_window": platform.system() != "Windows",
        "signed": False,
    }
    metrics_path = output_root / "desktop-metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
