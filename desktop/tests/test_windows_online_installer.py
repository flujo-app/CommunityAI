"""Exercise the PowerShell build boundary without downloading or running setup."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "desktop/installers/build_windows_online_installer.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class WindowsOnlineSilentFailureContracts(unittest.TestCase):
    """Static dialog-boundary guard; native download acceptance remains separate."""

    def test_script_error_dialogs_are_confined_to_the_interactive_reporter(self):
        source = (BUILDER.parent / "communityai-online.iss").read_text(encoding="utf-8")
        reporter = re.search(r"procedure ReportFailure\([^\n]+\);\s*begin\s*(.*?)\s*end;", source, re.S)
        self.assertIsNotNone(reporter)
        self.assertRegex(reporter.group(1), r"Log\(Message\);")
        self.assertRegex(
            reporter.group(1),
            r"if not WizardSilent then\s+SuppressibleMsgBox\(Message, mbError, MB_OK, IDOK\);",
        )
        remainder = source[: reporter.start()] + source[reporter.end() :]
        self.assertNotRegex(
            remainder, r"\b(?:SuppressibleMsgBox|MsgBox|TaskDialogMsgBox|SuppressibleTaskDialogMsgBox)\s*\("
        )

    def test_missing_verification_exits_before_execution_with_a_failure_code(self):
        source = (BUILDER.parent / "communityai-online.iss").read_text(encoding="utf-8")
        handoff = source.split("procedure CurStepChanged", 1)[1]
        guard = re.search(r"if not DownloadVerified then begin\s*(.*?)\s*end;", handoff, re.S)
        self.assertIsNotNone(guard)
        self.assertRegex(guard.group(1), r"ChildExitCode := 1;")
        self.assertRegex(guard.group(1), r"ReportFailure\([\s\S]+\);\s*Exit;")
        self.assertLess(guard.end(), handoff.index("ChildStarted := Exec("))
        self.assertNotIn("RaiseException(", handoff)
        self.assertRegex(handoff, r"function GetCustomSetupExitCode: Integer;\s*begin\s*Result := ChildExitCode;")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell builder")
class WindowsOnlineInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="communityai online test ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / "output with spaces"
        self.artifact = {
            "platform": "windows-x64",
            "kind": "offline-installer",
            "format": "exe",
            "version": "0.0.0-test",
            "filename": "communityai-0.0.0-test-windows-setup.exe",
            "url": "https://example.invalid/releases/0.0.0-test/communityai-0.0.0-test-windows-setup.exe",
            "sha256": "a" * 64,
            "size_bytes": 2519046440,
            "publisher": "CommunityAI test fixture",
        }

    def invoke(self, *extra):
        manifest = self.directory / "release fixture.json"
        manifest.write_text(json.dumps({"schema_version": 1, "artifacts": {"windows-x64": self.artifact}}))
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(BUILDER),
                "-Manifest",
                str(manifest),
                "-OutputDirectory",
                str(self.output),
                "-PythonCommand",
                sys.executable,
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def compiler(self, *, fails=False):
        compiler = self.directory / "record compiler.ps1"
        compiler.write_text(
            "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CompilerArguments)\n"
            "$definitions = @{}\n"
            "foreach ($argument in $CompilerArguments) {\n"
            "  if ($argument.StartsWith('/D')) {\n"
            "    $pieces = $argument.Substring(2).Split('=',2)\n"
            "    $definitions[$pieces[0]] = $pieces[1]\n"
            "  }\n"
            "}\n"
            "$destination = $definitions.OutputPath\n"
            "$CompilerArguments | ConvertTo-Json | Set-Content -LiteralPath "
            "(Join-Path $destination 'compiler-arguments.json') -Encoding utf8\n"
            + (
                "exit 7\n"
                if fails
                else "$fixture = Join-Path $destination ('communityai-' + $definitions.AppVersion + "
                "'-windows-online-setup.exe')\n"
                "[IO.File]::WriteAllBytes($fixture, [Text.Encoding]::UTF8.GetBytes('not executable - build fixture'))\n"
                "exit 0\n"
            )
        )
        return compiler

    def test_validation_preserves_exact_large_size_without_building(self):
        result = self.invoke("-UnsignedAlpha", "-ValidateOnly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.artifact)
        self.assertFalse(self.output.exists())

    def test_explicit_signing_choice_is_required_before_output(self):
        result = self.invoke("-ValidateOnly")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UnsignedAlpha", result.stderr)
        self.assertFalse(self.output.exists())

    def test_preprocessor_delimiters_in_publisher_are_rejected(self):
        for publisher in ("owner's label", "{unsafe}"):
            with self.subTest(publisher=publisher):
                self.artifact["publisher"] = publisher
                result = self.invoke("-UnsignedAlpha", "-ValidateOnly")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_mutable_or_untrusted_url_is_rejected_before_compiler(self):
        for url in (
            self.artifact["url"].replace("https:", "http:"),
            self.artifact["url"] + "?latest=true",
            self.artifact["url"] + "#fragment",
        ):
            with self.subTest(url=url):
                self.artifact["url"] = url
                result = self.invoke("-UnsignedAlpha", "-ValidateOnly")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_metadata_and_compiler_arguments_bind_the_pinned_offline_setup(self):
        result = self.invoke("-UnsignedAlpha", "-Compiler", str(self.compiler()))
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads((self.output / "compiler-arguments.json").read_text(encoding="utf-8-sig"))
        for key, value in {
            "InstallerUrl": self.artifact["url"],
            "InstallerFilename": self.artifact["filename"],
            "InstallerSha256": self.artifact["sha256"],
            "InstallerSize": str(self.artifact["size_bytes"]),
            "OutputPath": str(self.output),
        }.items():
            self.assertIn(f"/D{key}={value}", arguments)
        installer = self.output / "communityai-0.0.0-test-windows-online-setup.exe"
        metadata = json.loads(installer.with_suffix(".exe.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(metadata["offline_installer"], self.artifact)
        self.assertEqual(metadata["sha256"], hashlib.sha256(installer.read_bytes()).hexdigest())
        self.assertFalse(metadata["live_download_verified"])

    def test_failed_compiler_cannot_create_success_metadata(self):
        result = self.invoke("-UnsignedAlpha", "-Compiler", str(self.compiler(fails=True)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compiler failed: 7", result.stderr)
        self.assertEqual(list(self.output.glob("*.exe.json")), [])

    def test_existing_online_installer_is_not_overwritten(self):
        self.output.mkdir()
        original = self.output / "communityai-0.0.0-test-windows-online-setup.exe"
        original.write_bytes(b"preserved fixture")
        result = self.invoke("-UnsignedAlpha", "-Compiler", str(self.compiler()))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(original.read_bytes(), b"preserved fixture")
        self.assertFalse((self.output / "compiler-arguments.json").exists())


if __name__ == "__main__":
    unittest.main()
