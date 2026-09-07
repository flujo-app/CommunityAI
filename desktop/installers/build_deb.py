"""Package an already verified Linux runtime; keep per-user state outside dpkg ownership."""

import argparse
import errno
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def build(bundle, output, version, maintainer):
    bundle, output = bundle.resolve(), output.resolve()
    if not (bundle / "CommunityAI").is_file() or not (bundle / "node/CommunityAI-Node").is_file():
        raise ValueError("Expected the complete verified Linux CommunityAI bundle")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+~\-]*", version):
        raise ValueError("Invalid Debian version")
    if not re.fullmatch(r"[^<>\r\n]+ <[^<>\s]+@[^<>\s]+>", maintainer):
        raise ValueError("Maintainer must have a name and email address")
    output.mkdir(parents=True, exist_ok=True)
    scripts = Path(__file__).resolve().parent

    def link_or_copy(source, destination):
        try:
            return os.link(source, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            return shutil.copy2(source, destination)

    with tempfile.TemporaryDirectory(prefix="communityai-deb-", dir=bundle.parent) as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        app = root / "opt/communityai"
        # Hardlinks avoid duplicating several GiB of verified CUDA libraries.
        shutil.copytree(bundle, app, symlinks=True, copy_function=link_or_copy)
        (app / ".communityai-installation").unlink(missing_ok=True)
        shutil.copyfile(scripts / "installation-marker.txt", app / ".communityai-installation")
        control = root / "DEBIAN"
        control.mkdir()
        size_kib = sum(path.stat().st_size for path in app.rglob("*") if path.is_file()) // 1024
        (control / "control").write_text(
            f"Package: communityai\nVersion: {version}\nArchitecture: amd64\n"
            f"Maintainer: {maintainer}\nInstalled-Size: {size_kib}\n"
            "Section: science\nPriority: optional\nPre-Depends: python3 (>= 3.9)\n"
            "Depends: libc6 (>= 2.35), libstdc++6, libdbus-1-3, libegl1, libgl1, libglib2.0-0, "
            "libfontconfig1, libfreetype6, libxkbcommon0, libxkbcommon-x11-0, libxcb-cursor0, "
            "libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libwayland-cursor0\n"
            "Recommends: gnome-keyring\nHomepage: https://github.com/flujo-app/CommunityAI\n"
            "Description: CommunityAI public inference alpha\n"
            " Local OpenAI-compatible inference with optional community model sharing.\n"
            " Model weights download on demand. Settings and cache survive removal.\n",
            encoding="utf-8",
        )
        for name in ("preinst", "prerm"):
            shutil.copyfile(scripts / "linux_maintenance.py", control / name)
            (control / name).chmod(0o755)
        executable = root / "usr/bin/communityai"
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/sh\nexec /opt/communityai/CommunityAI "$@"\n', encoding="utf-8")
        executable.chmod(0o755)
        desktop = root / "usr/share/applications/communityai.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(
            "[Desktop Entry]\nType=Application\nName=CommunityAI\n"
            "Comment=Local and community AI inference\nExec=communityai\n"
            "Terminal=false\nCategories=Science;Utility;\n",
            encoding="utf-8",
        )
        target = output / f"communityai_{version}_amd64.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "-z1", "--build", str(root), str(target)], check=True)
        subprocess.run(["dpkg-deb", "--info", str(target)], check=True)
    print(json.dumps({"artifact": str(target), "version": version, "settings_cache_policy": "preserved"}))
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--maintainer", required=True)
    args = parser.parse_args()
    build(args.bundle, args.output, args.version, args.maintainer)
