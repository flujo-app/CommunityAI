"""Real hidden Inno downloads of a tiny harmless child from a loopback fixture.

Only the separately compiled helper enables WINDOWS_DOWNLOAD_TEST. No product
installer, public transfer, visible window, model or native credential is used.
"""

import ctypes
import hashlib
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "desktop/installers/communityai-online.iss"
HELPER = ROOT / "desktop/installers/WindowsDownload.cs"
CHILD_SOURCE = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
internal static class HarmlessChild {
    private static int Main(string[] args) {
        string directory = null;
        foreach (string arg in args)
            if (arg.StartsWith("/DIR=", StringComparison.OrdinalIgnoreCase)) directory = arg.Substring(5);
        if (directory == null || !Directory.Exists(directory)) return 99;
        string executable = Process.GetCurrentProcess().MainModule.FileName;
        File.WriteAllLines(Path.Combine(directory, "arguments.txt"), args);
        File.WriteAllText(Path.Combine(directory, "executable.txt"), executable);
        File.WriteAllText(Path.Combine(directory, "started"), "started");
        Thread.Sleep(1200);
        File.WriteAllText(Path.Combine(directory, "finished"), File.Exists(executable) ? "alive" : "missing");
        return 7;
    }
}
"""


class DownloadFixture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.requests.append(dict(self.headers))
        offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
        self.send_response(206 if offset else 200)
        if offset:
            self.send_header("Content-Range", f"bytes {offset}-{len(server.payload) - 1}/{len(server.payload)}")
        self.send_header("Content-Length", str(len(server.payload) - offset))
        self.end_headers()
        try:
            if server.mode == "held":
                self.wfile.write(server.payload[offset : offset + 32])
                self.wfile.flush()
                server.entered.set()
                server.release.wait(10)
            elif server.mode == "resume" and not offset:
                self.wfile.write(server.payload[: server.cut])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
            elif server.mode == "slow":
                step = max(1, len(server.payload) // 40)
                for start in range(offset, len(server.payload), step):
                    self.wfile.write(server.payload[start : start + step])
                    self.wfile.flush()
                    time.sleep(0.075)
            else:
                self.wfile.write(server.payload[offset:])
        except OSError:
            pass


class OwnedProcess:
    """Retain the opened process handle; never terminate by a reused PID."""

    def __init__(self, process, kernel, wintypes):
        self.pid = process.pid
        self.kernel = kernel
        self.handle = kernel.OpenProcess(0x100000 | 0x1000 | 1, False, self.pid)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess")
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in values)):
                raise OSError(ctypes.get_last_error(), "GetProcessTimes")
            self.created = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
            if abs(self.created / 10_000_000 - 11_644_473_600 - process.create_time()) > 0.002:
                raise RuntimeError("Process identity changed before its handle was retained")
            self.arguments = process.cmdline()
        except BaseException:
            kernel.CloseHandle(self.handle)
            raise

    def stopped(self):
        status = self.kernel.WaitForSingleObject(self.handle, 0)
        if status not in (0, 258):
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject")
        return status == 0

    def terminate(self):
        if not self.stopped() and not self.kernel.TerminateProcess(self.handle, 91):
            raise OSError(ctypes.get_last_error(), "TerminateProcess")

    def close(self):
        self.kernel.CloseHandle(self.handle)

    def exit_code(self):
        code = ctypes.c_uint32()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess")
        return code.value


@unittest.skipUnless(sys.platform == "win32", "Requires Windows Inno and .NET Framework")
class WindowsOnlineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ctypes import wintypes

        import psutil

        cls.psutil = psutil
        cls.wintypes = wintypes
        cls.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        cls.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        cls.kernel.OpenProcess.restype = wintypes.HANDLE
        cls.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        cls.kernel.GetProcessTimes.restype = wintypes.BOOL
        cls.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        cls.kernel.WaitForSingleObject.restype = wintypes.DWORD
        cls.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        cls.kernel.TerminateProcess.restype = wintypes.BOOL
        cls.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        cls.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        cls.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        cls.kernel.CloseHandle.restype = wintypes.BOOL
        cls.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        cls.kernel.CreateFileW.restype = wintypes.HANDLE
        explicit = os.environ.get("COMMUNITYAI_INNO_COMPILER")
        cls.inno = Path(explicit or shutil.which("ISCC.exe") or ROOT / ".gate13-runs/inno-setup-6/ISCC.exe")
        cls.csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not cls.inno.is_file() or not cls.csc.is_file():
            raise unittest.SkipTest("Set COMMUNITYAI_INNO_COMPILER to Inno6.7.3; Framework csc is also required")
        cls.compiled = tempfile.TemporaryDirectory(prefix="communityai-inno-fixture-")
        cls.addClassCleanup(cls.compiled.cleanup)
        cls.build = Path(cls.compiled.name)
        cls.helper = cls.build / "WindowsDownload.exe"
        child_source = cls.build / "HarmlessChild.cs"
        child_source.write_text(CHILD_SOURCE, encoding="utf-8")
        cls.child = cls.build / "harmless-child.exe"
        for source, output, defines in (
            (HELPER, cls.helper, ["/define:WINDOWS_DOWNLOAD_TEST"]),
            (child_source, cls.child, []),
        ):
            result = subprocess.run(
                [str(cls.csc), "/nologo", "/target:winexe", "/out:" + str(output), *defines, str(source)],
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise AssertionError(result.stdout.decode(errors="replace"))
        cls.payload = cls.child.read_bytes()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="communityai-inno-case-")
        self.directory = Path(self.temporary.name)
        self.markers = self.directory / "child markers"
        self.markers.mkdir()
        self.temp_root = self.directory / "inno-temp"
        self.temp_root.mkdir()
        self.owned = {}
        self.outer = None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DownloadFixture)
        self.server.daemon_threads = True
        self.server.payload = self.payload
        self.server.cut = len(self.payload) // 2
        self.server.requests = []
        self.server.mode = "resume"
        self.server.entered = threading.Event()
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)
        result = subprocess.run(
            [
                str(self.inno),
                "/Qp",
                "/DAppVersion=0.1.0-test",
                f"/DInstallerUrl=http://127.0.0.1:{self.server.server_port}/harmless-child.exe",
                "/DInstallerFilename=harmless-child.exe",
                "/DInstallerSha256=" + hashlib.sha256(self.payload).hexdigest(),
                "/DInstallerSize=" + str(len(self.payload)),
                "/DDownloadHelper=" + str(self.helper),
                "/DOutputPath=" + str(self.directory),
                "/DPublisherName=Local integration fixture",
                str(SCRIPT),
            ],
            capture_output=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))
        self.wrapper = self.directory / "communityai-0.1.0-test-windows-online-setup.exe"

    def remember(self, process):
        if process.pid in self.owned:
            return
        self.owned[process.pid] = OwnedProcess(process, self.kernel, self.wintypes)

    def refresh(self):
        for owned in list(self.owned.values()):
            if owned.stopped():
                continue
            try:
                parent = self.psutil.Process(owned.pid)
                for process in parent.children(recursive=True):
                    try:
                        self.remember(process)
                    except (self.psutil.NoSuchProcess, OSError):
                        pass
            except self.psutil.NoSuchProcess:
                pass

    def wait_until(self, predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.refresh()
            if predicate():
                return
            time.sleep(0.02)
        log = self.directory / "wrapper.log"
        detail = log.read_text(errors="replace")[-6000:] if log.exists() else "No wrapper log"
        self.fail("Timed out waiting for owned fixture state. " + detail)

    def launch(self, mode="resume"):
        self.server.mode = mode
        self.forwarded = [
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/CURRENTUSER",
            "/DIR=" + str(self.markers),
            "/GROUP=CommunityAI local fixture",
        ]
        environment = os.environ.copy()
        environment.update(TEMP=str(self.temp_root), TMP=str(self.temp_root))
        self.outer = subprocess.Popen(
            [str(self.wrapper), *self.forwarded, "/LOG=" + str(self.directory / "wrapper.log")],
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.remember(self.psutil.Process(self.outer.pid))

    def helper_process(self):
        return next(
            (
                owned
                for owned in self.owned.values()
                if owned.arguments and Path(owned.arguments[0]).name == "WindowsDownload.exe"
            ),
            None,
        )

    def assert_finished(self, expected=None):
        self.wait_until(lambda: self.outer.poll() is not None)
        if expected is not None:
            self.assertEqual(self.outer.returncode, expected)
        self.wait_until(lambda: all(owned.stopped() for owned in self.owned.values()), seconds=5)

    def cleanup(self):
        self.server.release.set()
        self.refresh()
        for owned in reversed(list(self.owned.values())):
            owned.terminate()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(owned.stopped() for owned in self.owned.values()):
            time.sleep(0.02)
        stopped = all(owned.stopped() for owned in self.owned.values())
        for owned in self.owned.values():
            owned.close()
        if self.outer is not None:
            self.outer.wait(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if stopped:
            self.temporary.cleanup()
        self.assertTrue(stopped, "Only owned fixture processes were stopped; temporary evidence retained on failure")

    def test_resume_handoff_arguments_exit_code_and_temporary_lifetime(self):
        self.launch()
        self.wait_until(lambda: (self.markers / "started").exists())
        self.assertIsNone(self.outer.poll())
        downloaded = Path((self.markers / "executable.txt").read_text())
        self.assertTrue(downloaded.is_relative_to(self.temp_root))
        self.assertTrue(downloaded.is_file())
        self.assertEqual((self.markers / "arguments.txt").read_text().splitlines(), self.forwarded + ["/LOG"])
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.requests[1]["Range"], f"bytes={self.server.cut}-")
        self.assert_finished(expected=7)
        self.assertEqual((self.markers / "finished").read_text(), "alive")
        self.assertFalse(downloaded.exists())
        self.assertFalse(downloaded.parent.exists())
        self.assertIsNotNone(self.helper_process())

    def test_cancel_file_stops_held_download_without_child(self):
        self.launch("held")
        self.wait_until(lambda: self.server.entered.is_set() and self.helper_process() is not None)
        helper = self.helper_process()
        cancel = Path(helper.arguments[6])
        output = Path(helper.arguments[2])
        self.assertTrue(cancel.is_relative_to(self.temp_root))
        cancel.write_text("cancel", encoding="ascii")
        self.assert_finished()
        self.assertNotEqual(self.outer.returncode, 0)
        self.assertEqual(helper.exit_code(), 3)
        self.assertFalse((self.markers / "started").exists())
        self.assertFalse(output.exists())

    def test_long_progress_reader_lock_does_not_block_verified_handoff(self):
        self.launch("slow")
        self.wait_until(
            lambda: self.helper_process() is not None and Path(self.helper_process().arguments[5]).is_file()
        )
        progress = Path(self.helper_process().arguments[5])
        self.assertTrue(progress.is_relative_to(self.temp_root))
        handle = self.kernel.CreateFileW(str(progress), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(handle, self.wintypes.HANDLE(-1).value)
        started = time.monotonic()
        try:
            # Inno's real reader permits reads but denies delete sharing. Keep
            # that lock beyond the helper's former one-second fatal threshold,
            # and observe the warning proving a publication attempt failed.
            self.wait_until(
                lambda: time.monotonic() - started >= 1.4 and Path(str(progress) + ".warning").exists(),
                seconds=5,
            )
            self.assertIsNone(self.outer.poll())
        finally:
            self.kernel.CloseHandle(handle)
        # Release before the child exits so the fixture cannot itself prevent
        # Inno from deleting the temporary directory during normal cleanup.
        self.wait_until(lambda: (self.markers / "started").exists())
        downloaded = Path((self.markers / "executable.txt").read_text())
        self.assertTrue(downloaded.is_relative_to(self.temp_root))
        self.assertTrue(downloaded.is_file())
        self.assertIsNone(self.outer.poll())
        self.assertEqual((self.markers / "arguments.txt").read_text().splitlines(), self.forwarded + ["/LOG"])
        self.assert_finished(expected=7)
        self.assertEqual((self.markers / "finished").read_text(), "alive")
        self.assertFalse(downloaded.exists())
        self.assertFalse(downloaded.parent.exists())

    def test_engine_exit_kills_owned_helper_without_child(self):
        self.launch("held")
        self.wait_until(lambda: self.server.entered.is_set() and self.helper_process() is not None)
        helper = self.helper_process()
        engine_pid = int(helper.arguments[7])
        self.assertIn(engine_pid, self.owned)
        engine = self.owned[engine_pid]
        self.assertEqual(engine.created, int(helper.arguments[8]))
        engine.terminate()
        self.wait_until(helper.stopped, seconds=3)
        self.assert_finished()
        self.assertNotEqual(self.outer.returncode, 0)
        self.assertFalse((self.markers / "started").exists())


if __name__ == "__main__":
    unittest.main()
