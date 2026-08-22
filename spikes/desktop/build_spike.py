"""Build and runtime-smoke one independently packaged desktop shell."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def _directory_metrics(path: Path) -> tuple[int, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return sum(item.stat().st_size for item in files), len(files)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shell", required=True, choices=("pyside", "webview"))
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        parser.error(f"PyInstaller is not installed: {exc}")

    project = Path(__file__).resolve().parent
    output_root = (args.output_root or project / "dist" / "desktop-spike").resolve()
    source_root = project / "src"
    build_root = project / "build" / "desktop-spike" / args.shell
    launcher = project / f"launch_{args.shell}.py"
    display_name = "CommunityAI-PySide-Spike" if args.shell == "pyside" else "CommunityAI-Webview-Spike"
    bundle_root = output_root / args.shell / display_name

    pyinstaller_args = [
        str(launcher),
        "--name",
        display_name,
        "--onedir",
        "--noconfirm",
        "--clean",
        "--paths",
        str(source_root),
        "--distpath",
        str(output_root / args.shell),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root / "spec"),
        "--collect-data",
        "communityai_desktop_spike",
        "--hidden-import",
        f"communityai_desktop_spike.{args.shell}_shell",
    ]
    credential_backend = {
        "Windows": "keyring.backends.Windows",
        "Darwin": "keyring.backends.macOS",
        "Linux": "keyring.backends.SecretService",
    }.get(platform.system())
    if credential_backend:
        pyinstaller_args.extend(("--hidden-import", credential_backend))
    if args.shell == "webview":
        webview_backend = {
            "Windows": "webview.platforms.winforms",
            "Darwin": "webview.platforms.cocoa",
            "Linux": "webview.platforms.qt",
        }.get(platform.system())
        if webview_backend:
            pyinstaller_args.extend(("--hidden-import", webview_backend))

    PyInstaller.__main__.run(pyinstaller_args)

    executable = bundle_root / f"{display_name}{'.exe' if os.name == 'nt' else ''}"
    if not executable.is_file():
        raise RuntimeError(f"packaged executable was not created: {executable}")
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    environment.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    if platform.system() == "Linux" and args.shell == "webview":
        environment.setdefault("PYWEBVIEW_GUI", "qt")
    check = subprocess.run(
        [str(executable), "--check-runtime"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    runtime = json.loads(check.stdout.strip())
    subprocess.run(
        [str(executable), "--ui-self-test"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    bundle_bytes, file_count = _directory_metrics(bundle_root)
    metrics = {
        "schema_version": 1,
        "shell": args.shell,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "bundle_bytes": bundle_bytes,
        "file_count": file_count,
        "runtime": runtime,
        "ui_smoke_passed": True,
        "signed": False,
    }
    metrics_path = output_root / f"{args.shell}-metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
