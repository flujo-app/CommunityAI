from __future__ import annotations

import os
import plistlib
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "desktop" / "src"))

from communityai_desktop.app import main
from communityai_desktop.pyside_shell import _single_instance_server_name
from communityai_desktop.startup import (
    LINUX_AUTOSTART_NAME,
    LOGIN_STARTUP_FLAG,
    MACOS_LAUNCH_AGENT_NAME,
    MAX_WINDOWS_RUN_COMMAND_CHARS,
    WINDOWS_RUN_KEY,
    WINDOWS_VALUE_NAME,
    LoginStartupError,
    login_startup_command,
    login_startup_enabled,
    set_login_startup,
)


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self):
        self.keys = set()
        self.values = {}
        self.closed = []
        self.create_error = None

    def __bool__(self):
        return False

    def CreateKeyEx(self, root, path, reserved, access):
        if self.create_error is not None:
            raise self.create_error
        self.keys.add(path)
        return (root, path)

    def OpenKey(self, root, path, reserved, access):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return (root, path)

    def CloseKey(self, key):
        self.closed.append(key)

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.values[(key[1], name)] = (value, value_type)

    def QueryValueEx(self, key, name):
        try:
            return self.values[(key[1], name)]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc

    def DeleteValue(self, key, name):
        try:
            del self.values[(key[1], name)]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc


class LoginStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.tmp_path = Path(self._temporary_directory.name)

    def test_source_and_frozen_commands_are_exact_and_absolute(self):
        executable = self.tmp_path / "Community AI" / "communityai"

        source = login_startup_command(executable, frozen=False)
        frozen = login_startup_command(executable, frozen=True)

        self.assertTrue(Path(source[0]).is_absolute())
        self.assertEqual(source[1:], ("-m", "communityai_desktop", LOGIN_STARTUP_FLAG))
        self.assertEqual(frozen, (os.path.abspath(executable), LOGIN_STARTUP_FLAG))

    def test_empty_or_control_character_command_is_rejected(self):
        for command in ((), ("app", ""), ("app\nother",), ("app\tother",), ("app\x7f",)):
            with self.subTest(command=command):
                with self.assertRaisesRegex(LoginStartupError, "single-line"):
                    login_startup_enabled(command=command, platform_name="Linux", home=self.tmp_path, environ={})
                with self.assertRaisesRegex(LoginStartupError, "single-line"):
                    set_login_startup(
                        True,
                        command=command,
                        platform_name="Linux",
                        home=self.tmp_path,
                        environ={},
                    )

    def test_linux_xdg_registration_round_trips_and_escapes_exec_literals(self):
        config_home = self.tmp_path / "xdg"
        command = ('/opt/Community AI/100%/$money`/"quoted"/app', LOGIN_STARTUP_FLAG)
        environ = {"XDG_CONFIG_HOME": str(config_home)}

        set_login_startup(
            True,
            command=command,
            platform_name="Linux",
            home=self.tmp_path / "home",
            environ=environ,
        )

        entry = config_home / "autostart" / LINUX_AUTOSTART_NAME
        rendered = entry.read_text(encoding="utf-8")
        self.assertIn("100%%", rendered)
        self.assertIn("\\$money", rendered)
        self.assertIn("\\`", rendered)
        self.assertIn('\\"quoted\\"', rendered)
        self.assertTrue(
            login_startup_enabled(
                command=command,
                platform_name="Linux",
                home=self.tmp_path / "home",
                environ=environ,
            )
        )

        entry.write_text(rendered + "# changed\n", encoding="utf-8")
        self.assertFalse(
            login_startup_enabled(
                command=command,
                platform_name="Linux",
                home=self.tmp_path / "home",
                environ=environ,
            )
        )
        set_login_startup(
            False,
            command=command,
            platform_name="Linux",
            home=self.tmp_path / "home",
            environ=environ,
        )
        set_login_startup(
            False,
            command=command,
            platform_name="Linux",
            home=self.tmp_path / "home",
            environ=environ,
        )
        self.assertFalse(entry.exists())

    def test_linux_relative_or_empty_xdg_config_home_falls_back_to_home(self):
        command = ("/opt/communityai", LOGIN_STARTUP_FLAG)
        for configured in ("relative/config", ""):
            with self.subTest(configured=configured):
                home = self.tmp_path / ("home-relative" if configured else "home-empty")
                set_login_startup(
                    True,
                    command=command,
                    platform_name="Linux",
                    home=home,
                    environ={"XDG_CONFIG_HOME": configured},
                )
                self.assertTrue((home / ".config" / "autostart" / LINUX_AUTOSTART_NAME).is_file())

    def test_macos_launch_agent_round_trips_exact_arguments(self):
        command = ("/Applications/CommunityAI.app/Contents/MacOS/CommunityAI", LOGIN_STARTUP_FLAG)

        set_login_startup(
            True,
            command=command,
            platform_name="Darwin",
            home=self.tmp_path,
            environ={},
        )

        entry = self.tmp_path / "Library" / "LaunchAgents" / MACOS_LAUNCH_AGENT_NAME
        document = plistlib.loads(entry.read_bytes())
        self.assertEqual(document["Label"], "org.communityai.desktop")
        self.assertEqual(document["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(document["ProgramArguments"], list(command))
        self.assertIs(document["RunAtLoad"], True)
        self.assertTrue(
            login_startup_enabled(
                command=command,
                platform_name="Darwin",
                home=self.tmp_path,
                environ={},
            )
        )

        set_login_startup(
            False,
            command=command,
            platform_name="Darwin",
            home=self.tmp_path,
            environ={},
        )
        self.assertFalse(entry.exists())

    def test_file_registration_rejects_live_and_dangling_symlinks(self):
        command = ("/opt/communityai", LOGIN_STARTUP_FLAG)
        autostart = self.tmp_path / ".config" / "autostart"
        autostart.mkdir(parents=True)
        entry = autostart / LINUX_AUTOSTART_NAME
        target = self.tmp_path / "outside.desktop"
        target.write_text("outside\n", encoding="utf-8")
        try:
            entry.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        with self.assertRaisesRegex(LoginStartupError, "safe regular file"):
            set_login_startup(
                True,
                command=command,
                platform_name="Linux",
                home=self.tmp_path,
                environ={},
            )
        target.unlink()
        with self.assertRaisesRegex(LoginStartupError, "safe regular file"):
            set_login_startup(
                True,
                command=command,
                platform_name="Linux",
                home=self.tmp_path,
                environ={},
            )

    def test_windows_registry_round_trip_is_exact_and_idempotent(self):
        registry = FakeRegistry()
        command = (r"C:\Program Files\CommunityAI\CommunityAI.exe", LOGIN_STARTUP_FLAG)

        self.assertFalse(login_startup_enabled(command=command, platform_name="Windows", registry=registry))
        set_login_startup(True, command=command, platform_name="Windows", registry=registry)
        self.assertTrue(login_startup_enabled(command=command, platform_name="Windows", registry=registry))
        value, value_type = registry.values[(WINDOWS_RUN_KEY, WINDOWS_VALUE_NAME)]
        self.assertEqual(value_type, registry.REG_SZ)
        self.assertIn('"C:\\Program Files\\CommunityAI\\CommunityAI.exe"', value)

        registry.values[(WINDOWS_RUN_KEY, WINDOWS_VALUE_NAME)] = (value, registry.REG_EXPAND_SZ)
        self.assertFalse(login_startup_enabled(command=command, platform_name="Windows", registry=registry))
        registry.values[(WINDOWS_RUN_KEY, WINDOWS_VALUE_NAME)] = ("different", registry.REG_SZ)
        self.assertFalse(login_startup_enabled(command=command, platform_name="Windows", registry=registry))

        set_login_startup(False, command=command, platform_name="Windows", registry=registry)
        set_login_startup(False, command=command, platform_name="Windows", registry=registry)
        self.assertNotIn((WINDOWS_RUN_KEY, WINDOWS_VALUE_NAME), registry.values)
        self.assertTrue(registry.closed)

    def test_windows_registry_errors_and_command_limit_fail_closed(self):
        registry = FakeRegistry()
        registry.create_error = PermissionError("denied")
        with self.assertRaisesRegex(LoginStartupError, "enable Windows"):
            set_login_startup(
                True,
                command=(r"C:\CommunityAI.exe", LOGIN_STARTUP_FLAG),
                platform_name="Windows",
                registry=registry,
            )

        long_argument = "x" * (MAX_WINDOWS_RUN_COMMAND_CHARS + 1)
        with self.assertRaisesRegex(LoginStartupError, "exceeds"):
            set_login_startup(
                True,
                command=(r"C:\CommunityAI.exe", long_argument),
                platform_name="Windows",
                registry=FakeRegistry(),
            )

    def test_unsupported_platform_and_non_boolean_write_fail_closed(self):
        with self.assertRaisesRegex(LoginStartupError, "not supported"):
            login_startup_enabled(command=("app",), platform_name="Plan9", home=self.tmp_path, environ={})
        with self.assertRaises(TypeError):
            set_login_startup(1, command=("app",), platform_name="Linux", home=self.tmp_path, environ={})

    def test_login_launch_is_minimized_and_does_not_activate_an_existing_instance(self):
        with patch("communityai_desktop.pyside_shell.run", return_value=0) as run:
            self.assertEqual(main([LOGIN_STARTUP_FLAG, "--no-manage-node"]), 0)

        self.assertTrue(run.call_args.kwargs["start_minimized"])
        self.assertFalse(run.call_args.kwargs["activate_existing_instance"])

    def test_existing_instance_protocol_distinguishes_manual_and_login_launch(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtNetwork import QLocalServer
            from PySide6.QtWidgets import QApplication
        except ModuleNotFoundError as exc:
            self.skipTest(f"PySide6 is unavailable: {exc}")

        from communityai_desktop.pyside_shell import run

        application = QApplication.instance() or QApplication([])
        name = f"communityai-test-{uuid.uuid4().hex}"
        QLocalServer.removeServer(name)
        server = QLocalServer(application)
        server.setSocketOptions(QLocalServer.UserAccessOption)
        self.assertTrue(server.listen(name), server.errorString())
        self.addCleanup(server.close)
        self.addCleanup(lambda: QLocalServer.removeServer(name))

        for activate, expected in ((True, b"activate"), (False, b"silent")):
            with self.subTest(activate=activate):
                self.assertEqual(
                    run(
                        connect=lambda: None,
                        single_instance=True,
                        activate_existing_instance=activate,
                        instance_name=name,
                    ),
                    0,
                )
                application.processEvents()
                if not server.hasPendingConnections():
                    server.waitForNewConnection(1_000)
                self.assertTrue(server.hasPendingConnections())
                socket = server.nextPendingConnection()
                if socket.bytesAvailable() == 0:
                    socket.waitForReadyRead(1_000)
                self.assertEqual(bytes(socket.readAll()).strip(), expected)
                socket.close()

    def test_primary_instance_releases_owned_endpoint_and_lock(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtNetwork import QLocalServer
        except ModuleNotFoundError as exc:
            self.skipTest(f"PySide6 is unavailable: {exc}")

        from communityai_desktop.pyside_shell import run

        name = f"communityai-owner-test-{uuid.uuid4().hex}"

        def unavailable():
            raise RuntimeError("offline for UI smoke")

        with patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False):
            self.assertEqual(
                run(
                    connect=unavailable,
                    auto_close_seconds=0.05,
                    single_instance=True,
                    instance_name=name,
                ),
                0,
            )
        probe = QLocalServer()
        self.assertTrue(probe.listen(name), probe.errorString())
        probe.close()
        QLocalServer.removeServer(name)

    def test_single_instance_name_is_stable_and_scoped_to_data_location(self):
        first = _single_instance_server_name(self.tmp_path / "one")
        same = _single_instance_server_name(self.tmp_path / "one")
        other = _single_instance_server_name(self.tmp_path / "two")

        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^communityai-desktop-[0-9a-f]{20}$")


if __name__ == "__main__":
    unittest.main()
