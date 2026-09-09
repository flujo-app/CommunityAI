"""Actual tiny Inno upgrade through UpdateManager; never touches the real installation."""

import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from communityai_desktop.updater import UpdateManager


@unittest.skipUnless(
    sys.platform == "win32" and os.environ.get("COMMUNITYAI_INNO_COMPILER"), "Requires Windows and Inno"
)
class UpdateInstallerTests(unittest.TestCase):
    def test_verified_update_stops_old_installation_replaces_payload_and_reopens(self):
        compiler = Path(os.environ["COMMUNITYAI_INNO_COMPILER"])
        csc = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        installer_source = Path(__file__).resolve().parents[1] / "installers/communityai.iss"
        with tempfile.TemporaryDirectory(prefix="communityai-update-test-") as directory:
            root = Path(directory)
            bundle, install, output = (root / name for name in ("bundle", "installed", "output"))
            for path in (bundle, install, output):
                path.mkdir()
            source = root / "App.cs"
            source.write_text(
                r"""
using System; using System.IO; using System.Diagnostics;
class App { static int Main(string[] args) {
 string root = Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
 File.AppendAllText(Path.Combine(root,"lifecycle.txt"), args.Length > 0 && args[0] == "--prepare-update" ? "stop\n" : "open\n");
 return 0;
} }
"""
            )
            real_popen = subprocess.Popen
            flags = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                [str(csc), "/nologo", "/target:winexe", "/out:" + str(bundle / "CommunityAI.exe"), str(source)],
                check=True,
                capture_output=True,
                creationflags=flags,
            )
            import shutil

            shutil.copy2(bundle / "CommunityAI.exe", install / "CommunityAI.exe")
            (install / ".communityai-installation").write_text("CommunityAI installer-managed fixture")
            (install / "_internal").mkdir()
            (install / "_internal/obsolete.dll").write_bytes(b"old")
            (install / "settings.json").write_text("keep settings")
            (bundle / "_internal").mkdir()
            (bundle / "_internal/current.dll").write_bytes(b"new")
            identifier = "CommunityAI.UpdateFixture." + uuid.uuid4().hex
            result = subprocess.run(
                [
                    str(compiler),
                    "/Qp",
                    "/DBundleDir=" + str(bundle),
                    "/DOutputPath=" + str(output),
                    "/DAppVersion=0.1.0-alpha.20260909.3",
                    "/DAppIdentifier=" + identifier,
                    str(installer_source),
                ],
                capture_output=True,
                creationflags=flags,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
            package = next(output.glob("*.exe"))
            item = {"size_bytes": package.stat().st_size, "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}
            manager = UpdateManager(root / "cache", root=install, system="Windows")
            manager.directory.mkdir()
            manager.candidate = {"artifacts": {"windows-x64": item}, "expires_at": int(time.time()) + 60}, package
            arguments = []

            def hidden_setup(argv, **kwargs):
                arguments.append(list(argv))
                # Production /SILENT displays progress; this fixture must never display windows.
                argv = ["/VERYSILENT" if arg == "/SILENT" else arg for arg in argv] + ["/NOICONS"]
                return real_popen(argv, **kwargs)

            try:
                with patch("communityai_desktop.updater.subprocess.Popen", side_effect=hidden_setup):
                    manager.install()
                    manager.thread.join(60)
                self.assertFalse(manager.thread.is_alive())
                self.assertEqual(manager.process.returncode, 0, manager.snapshot())
                deadline = time.monotonic() + 10
                while "open" not in (install / "lifecycle.txt").read_text() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual((install / "lifecycle.txt").read_text(), "stop\nopen\n")
                self.assertEqual((install / "settings.json").read_text(), "keep settings")
                self.assertFalse((install / "_internal/obsolete.dll").exists())
                self.assertEqual((install / "_internal/current.dll").read_bytes(), b"new")
                self.assertIn("/UPDATE=1", arguments[0])
            finally:
                uninstaller = install / "unins000.exe"
                if uninstaller.exists():
                    subprocess.run(
                        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                        creationflags=flags,
                        timeout=60,
                        check=True,
                    )


if __name__ == "__main__":
    unittest.main()
