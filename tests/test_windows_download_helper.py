"""Tiny real HTTP fixtures for the separately compiled Windows downloader test build.

Production is independently compiled without the loopback/test timing symbols.
No public requests, full installers, models or visible windows are launched.
"""

import ctypes
import hashlib
import http.server
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "desktop/installers/WindowsDownload.cs"
PAYLOAD = bytes(range(256)) * 1024
HASH = hashlib.sha256(PAYLOAD).hexdigest()


def creation_time(pid):
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    assert handle, ctypes.get_last_error()
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values))
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


class Fixture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.requests.append(dict(self.headers))
        mode = server.mode
        offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
        if mode == "retry_exhausted":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        drop = mode in {"resume", "wrong_start", "wrong_total", "wrong_end", "ignored_range"} and offset == 0
        resumed = offset > 0 and mode != "ignored_range"
        self.send_response(206 if resumed else 200)
        if resumed:
            first = offset + (mode == "wrong_start")
            last = len(PAYLOAD) - 1 - (mode == "wrong_end")
            total = len(PAYLOAD) + (mode == "wrong_total")
            self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
        length = len(PAYLOAD) - offset
        if mode == "bad_length":
            length -= 1
        self.send_header("Content-Length", str(length))
        if mode == "encoding":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        try:
            if mode == "stall":
                self.wfile.write(PAYLOAD[:1024])
                self.wfile.flush()
                server.entered.set()
                server.release.wait(10)
                return
            if drop:
                self.wfile.write(PAYLOAD[:90000])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            if mode == "slow":
                for start in range(offset, len(PAYLOAD), 4096):
                    self.wfile.write(PAYLOAD[start : start + 4096])
                    self.wfile.flush()
                    time.sleep(0.02)
            else:
                self.wfile.write(PAYLOAD[offset:])
        except (OSError, ConnectionError):
            pass


@unittest.skipUnless(sys.platform == "win32", "Requires Windows .NET Framework and Win32 process handles")
class WindowsDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled = tempfile.TemporaryDirectory(prefix="communityai-download-tests-")
        cls.build = Path(cls.compiled.name)
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            raise unittest.SkipTest("Windows .NET Framework compiler unavailable")
        cls.executables = {}
        for name, symbols in (
            ("production", []),
            ("test", ["WINDOWS_DOWNLOAD_TEST"]),
            ("deadline", ["WINDOWS_DOWNLOAD_TEST", "WINDOWS_DOWNLOAD_SHORT_DEADLINE"]),
        ):
            executable = cls.build / (name + ".exe")
            command = [str(compiler), "/nologo", "/target:winexe", "/out:" + str(executable)]
            if symbols:
                command.append("/define:" + ";".join(symbols))
            command.append(str(SOURCE))
            result = subprocess.run(command, capture_output=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stdout.decode(errors="replace"))
            cls.executables[name] = executable
        probe_source = cls.build / "RangeProbe.cs"
        probe_source.write_text(
            """using System;
using System.Reflection;
internal static class RangeProbe {
  private static int Main(string[] args) {
    try {
      typeof(WindowsDownload).GetMethod("ValidateRangeHeader", BindingFlags.NonPublic | BindingFlags.Static)
        .Invoke(null, new object[] { args[0], Int64.Parse(args[1]), Int64.Parse(args[2]) });
      return 0;
    } catch { return 1; }
  }
}
"""
        )
        cls.range_probe = cls.build / "range-probe.exe"
        subprocess.run(
            [
                str(compiler),
                "/nologo",
                "/target:winexe",
                "/main:RangeProbe",
                "/out:" + str(cls.range_probe),
                str(SOURCE),
                str(probe_source),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.compiled.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="communityai-download-case-")
        self.directory = Path(self.temporary.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.server.daemon_threads = True
        self.server.mode = "full"
        self.server.requests = []
        self.server.entered = threading.Event()
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.children = []

    def tearDown(self):
        self.server.release.set()
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def launch(self, mode="full", binary="test", sha=HASH, parent=None, parent_time=None, size=None):
        self.server.mode = mode
        self.output = self.directory / "package.exe"
        self.progress = self.directory / "progress.txt"
        self.cancel = self.directory / "cancel"
        parent = os.getpid() if parent is None else parent
        argv = [
            str(self.executables[binary]),
            f"http://127.0.0.1:{self.server.server_port}/package.exe",
            str(self.output),
            str(len(PAYLOAD) if size is None else size),
            sha,
            str(self.progress),
            str(self.cancel),
            str(parent),
            str(creation_time(parent) if parent_time is None else parent_time),
        ]
        child = subprocess.Popen(argv, creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(child)
        return child

    def finish(self, child, expected=0, progress_verified=True):
        self.assertEqual(child.wait(timeout=12), expected)
        progress = self.progress.read_text()
        self.assertRegex(progress, r"\A[0-9]+\|[0-9]+\|[0-5]\Z")
        self.assertFalse(Path(str(self.progress) + ".new").exists())
        if expected == 0:
            self.assertEqual(self.output.read_bytes(), PAYLOAD)
            if progress_verified:
                self.assertEqual(progress, f"{len(PAYLOAD)}|{len(PAYLOAD)}|3")
        else:
            self.assertFalse(self.output.exists())
            detail = Path(str(self.progress) + ".error").read_text(encoding="utf-8")
            self.assertTrue(0 < len(detail) <= 1024)
            self.assertNotIn("http://", detail)
            self.assertNotIn("\n", detail)

    def test_exact_download_and_identity_encoding(self):
        self.finish(self.launch())
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["Accept-Encoding"], "identity")
        self.assertEqual(self.server.requests[0]["User-Agent"], "CommunityAI-Online-Installer/1")

    def test_interrupted_stream_resumes_exact_offset(self):
        self.finish(self.launch("resume"))
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.requests[1]["Range"], "bytes=90000-")

    def test_wrong_range_start_is_rejected(self):
        self.finish(self.launch("wrong_start"), 1)
        self.assertEqual(len(self.server.requests), 2)

    def test_wrong_range_end_is_rejected(self):
        self.finish(self.launch("wrong_end"), 1)

    def test_wrong_range_total_is_rejected(self):
        self.finish(self.launch("wrong_total"), 1)

    def test_ignored_resume_range_is_rejected(self):
        self.finish(self.launch("ignored_range"), 1)

    def test_wrong_size_is_rejected_before_acceptance(self):
        self.finish(self.launch("bad_length"), 1)

    def test_hash_mismatch_removes_download(self):
        self.finish(self.launch(sha="0" * 64), 1)

    def test_redirect_is_never_followed(self):
        self.finish(self.launch("redirect"), 1)
        self.assertEqual(len(self.server.requests), 1)

    def test_content_encoding_is_rejected(self):
        self.finish(self.launch("encoding"), 1)

    def test_retries_have_a_finite_request_budget(self):
        self.finish(self.launch("retry_exhausted"), 1)
        self.assertEqual(len(self.server.requests), 8)

    def test_production_build_rejects_loopback_http_without_request(self):
        child = self.launch(binary="production")
        self.assertEqual(child.wait(timeout=5), 1)
        self.assertFalse(self.server.requests)
        self.assertFalse(self.output.exists())

    def test_parent_creation_mismatch_prevents_request(self):
        self.finish(self.launch(parent_time=creation_time(os.getpid()) + 1), 1)
        self.assertFalse(self.server.requests)

    def test_cancellation_aborts_a_blocked_response(self):
        child = self.launch("stall")
        self.assertTrue(self.server.entered.wait(5))
        started = time.monotonic()
        self.cancel.write_text("cancel")
        self.finish(child, 3)
        self.assertLess(time.monotonic() - started, 2)

    def test_parent_exit_aborts_a_blocked_response(self):
        parent = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], creationflags=subprocess.CREATE_NO_WINDOW
        )
        self.children.append(parent)
        child = self.launch("stall", parent=parent.pid)
        self.assertTrue(self.server.entered.wait(5))
        parent.terminate()
        parent.wait(timeout=5)
        self.finish(child, 3)

    def test_monotonic_deadline_aborts_stalled_download(self):
        child = self.launch("stall", binary="deadline")
        self.finish(child, 2)

    def test_existing_output_is_preserved(self):
        existing = self.directory / "package.exe"
        existing.write_bytes(b"original")
        child = self.launch()
        self.assertEqual(child.wait(timeout=5), 1)
        self.assertEqual(existing.read_bytes(), b"original")
        self.assertFalse(self.server.requests)

    def test_progress_survives_transient_reader_delete_lock(self):
        from ctypes import wintypes

        child = self.launch("slow")
        until = time.monotonic() + 5
        while not self.progress.exists() and time.monotonic() < until:
            time.sleep(0.01)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.progress), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            time.sleep(0.45)
        finally:
            kernel.CloseHandle(handle)
        self.finish(child)

    def test_persistent_progress_reader_lock_cannot_abort_verified_download(self):
        from ctypes import wintypes

        child = self.launch("slow")
        until = time.monotonic() + 5
        while not self.progress.exists() and time.monotonic() < until:
            time.sleep(0.005)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.progress), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        started = time.monotonic()
        try:
            # Hold through helper exit, including all final progress updates.
            self.assertEqual(child.wait(timeout=12), 0)
            self.assertGreater(time.monotonic() - started, 1)
        finally:
            kernel.CloseHandle(handle)
        self.finish(child, progress_verified=False)
        warning = Path(str(self.progress) + ".warning").read_text()
        self.assertRegex(warning, r"Skipped progress frame: [A-Za-z]+ HRESULT [0-9A-F]{8}")

    def test_access_denied_progress_cannot_abort_verified_download(self):
        progress = self.directory / "progress.txt"
        progress.write_text(f"0|{len(PAYLOAD)}|0")
        os.chmod(progress, stat.S_IREAD)
        try:
            self.finish(self.launch(), progress_verified=False)
            self.assertEqual(progress.read_text(), f"0|{len(PAYLOAD)}|0")
            warning = Path(str(progress) + ".warning").read_text()
            self.assertIn("HRESULT 80070005", warning)
        finally:
            os.chmod(progress, stat.S_IREAD | stat.S_IWRITE)

    def test_unavailable_progress_never_bypasses_hash_rejection(self):
        progress = self.directory / "progress.txt"
        progress.write_text(f"0|{len(PAYLOAD)}|0")
        os.chmod(progress, stat.S_IREAD)
        try:
            self.finish(self.launch(sha="0" * 64), 1)
            self.assertEqual(progress.read_text(), f"0|{len(PAYLOAD)}|0")
            detail = Path(str(progress) + ".error").read_text()
            self.assertIn("SHA-256 or size mismatch", detail)
        finally:
            os.chmod(progress, stat.S_IREAD | stat.S_IWRITE)

    def test_progress_is_atomic_and_limited_to_four_updates_per_second(self):
        child = self.launch("slow")
        stamps = set()
        snapshots = []
        while child.poll() is None:
            try:
                snapshots.append(self.progress.read_text())
                stamps.add(self.progress.stat().st_mtime_ns)
            except (FileNotFoundError, PermissionError):
                pass
            time.sleep(0.005)
        self.finish(child)
        self.assertTrue(all(re.fullmatch(r"[0-9]+\|[0-9]+\|[0-5]", value) for value in snapshots))
        ordered = sorted(stamps)
        self.assertGreaterEqual(len(ordered), 3)
        self.assertTrue(all(after - before >= 240_000_000 for before, after in zip(ordered, ordered[1:])))

    def test_range_validation_preserves_offsets_above_two_gib(self):
        expected = 3 * 1024**3 + 17
        offset = 2 * 1024**3 + 123
        result = subprocess.run(
            [str(self.range_probe), f"bytes {offset}-{expected - 1}/{expected}", str(offset), str(expected)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0)

    def test_range_validation_rejects_overflow_and_truncation(self):
        expected = 3 * 1024**3 + 17
        offset = 2 * 1024**3 + 123
        for header in (f"bytes 123-{expected - 1}/{expected}", "bytes 0-9223372036854775808/9223372036854775809"):
            with self.subTest(header=header):
                result = subprocess.run(
                    [str(self.range_probe), header, str(offset), str(expected)],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
