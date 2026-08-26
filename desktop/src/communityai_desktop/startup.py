"""Cross-platform, per-user login-startup registration for the desktop app."""

from __future__ import annotations

import os
import platform
import plistlib
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

APP_NAME = "CommunityAI"
LOGIN_STARTUP_FLAG = "--started-at-login"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_VALUE_NAME = "CommunityAI"
MACOS_LAUNCH_AGENT_NAME = "org.communityai.desktop.plist"
LINUX_AUTOSTART_NAME = "communityai.desktop"
MAX_WINDOWS_RUN_COMMAND_CHARS = 260


class LoginStartupError(RuntimeError):
    """Raised when the per-user login-startup entry cannot be read or changed."""


class SingleInstanceError(RuntimeError):
    """Raised when exclusive per-user desktop ownership cannot be established."""


def login_startup_command(
    executable: Path | str | None = None,
    *,
    frozen: bool | None = None,
) -> tuple[str, ...]:
    """Return the exact argv registered for the current desktop installation."""

    resolved_executable = os.path.abspath(os.fspath(executable or sys.executable))
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    arguments = [resolved_executable]
    if not frozen:
        arguments.extend(("-m", "communityai_desktop"))
    arguments.append(LOGIN_STARTUP_FLAG)
    return tuple(arguments)


def _validated_arguments(command: Sequence[str] | None) -> tuple[str, ...]:
    arguments = tuple(login_startup_command() if command is None else command)
    if not arguments or any(
        not isinstance(argument, str)
        or not argument
        or any(ord(character) < 32 or ord(character) == 127 for character in argument)
        for argument in arguments
    ):
        raise LoginStartupError("Login-startup command arguments must be non-empty single-line strings")
    return arguments


def _windows_command(arguments: Sequence[str]) -> str:
    return subprocess.list2cmdline([os.fspath(argument) for argument in arguments])


def _desktop_exec_argument(value: str) -> str:
    # Desktop Entry Exec values are parsed without a shell. Quoting every argv
    # member avoids field-code or whitespace ambiguity; these are the characters
    # that require a backslash inside a quoted argument.
    escaped = value.replace("%", "%%").replace("\\", "\\\\")
    for character in ('"', "`", "$"):
        escaped = escaped.replace(character, f"\\{character}")
    return f'"{escaped}"'


def _linux_autostart_bytes(arguments: Sequence[str]) -> bytes:
    command = " ".join(_desktop_exec_argument(os.fspath(argument)) for argument in arguments)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        f"Name={APP_NAME}\n"
        "Comment=Start the local CommunityAI service after sign-in\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "NoDisplay=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    ).encode("utf-8")


def _macos_launch_agent_bytes(arguments: Sequence[str]) -> bytes:
    return plistlib.dumps(
        {
            "Label": "org.communityai.desktop",
            "LimitLoadToSessionType": "Aqua",
            "ProcessType": "Interactive",
            "ProgramArguments": [os.fspath(argument) for argument in arguments],
            "RunAtLoad": True,
        },
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def _registration_path(
    system: str,
    *,
    environ: Mapping[str, str],
    home: Path,
) -> Path:
    if system == "Darwin":
        return home / "Library" / "LaunchAgents" / MACOS_LAUNCH_AGENT_NAME
    if system == "Linux":
        config_home = environ.get("XDG_CONFIG_HOME")
        configured = Path(config_home).expanduser() if config_home else None
        base = configured if configured is not None and configured.is_absolute() else home / ".config"
        return base / "autostart" / LINUX_AUTOSTART_NAME
    raise LoginStartupError(f"Login startup is not supported on {system!r}")


def _write_registration(path: Path, content: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise LoginStartupError(f"Login-startup entry is not a safe regular file: {path}")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except LoginStartupError:
        raise
    except OSError as exc:
        raise LoginStartupError(f"Could not write login-startup entry {path}: {exc}") from exc


def _remove_registration(path: Path) -> bool:
    try:
        if not path.exists() and not path.is_symlink():
            return False
        if not path.is_file() or path.is_symlink():
            raise LoginStartupError(f"Login-startup entry is not a safe regular file: {path}")
        path.unlink()
        return True
    except LoginStartupError:
        raise
    except OSError as exc:
        raise LoginStartupError(f"Could not remove login-startup entry {path}: {exc}") from exc


def _read_registration(path: Path) -> bytes | None:
    try:
        if not path.exists() and not path.is_symlink():
            return None
        if not path.is_file() or path.is_symlink():
            raise LoginStartupError(f"Login-startup entry is not a safe regular file: {path}")
        return path.read_bytes()
    except LoginStartupError:
        raise
    except OSError as exc:
        raise LoginStartupError(f"Could not read login-startup entry {path}: {exc}") from exc


def _winreg_module():
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - only reachable on a broken Windows runtime
        raise LoginStartupError("The Windows registry API is unavailable") from exc
    return winreg


def _windows_login_startup_enabled(arguments: Sequence[str], *, registry=None) -> bool:
    if registry is None:
        registry = _winreg_module()
    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER,
            WINDOWS_RUN_KEY,
            0,
            registry.KEY_QUERY_VALUE,
        )
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LoginStartupError(f"Could not open the Windows login-startup registry key: {exc}") from exc
    try:
        try:
            value, value_type = registry.QueryValueEx(key, WINDOWS_VALUE_NAME)
        except FileNotFoundError:
            return False
        return value_type == registry.REG_SZ and value == _windows_command(arguments)
    except OSError as exc:
        raise LoginStartupError(f"Could not read the Windows login-startup entry: {exc}") from exc
    finally:
        registry.CloseKey(key)


def _set_windows_login_startup(enabled: bool, arguments: Sequence[str], *, registry=None) -> None:
    if registry is None:
        registry = _winreg_module()
    if enabled:
        command = _windows_command(arguments)
        if len(command) > MAX_WINDOWS_RUN_COMMAND_CHARS:
            raise LoginStartupError(f"Windows login-startup command exceeds {MAX_WINDOWS_RUN_COMMAND_CHARS} characters")
        try:
            key = registry.CreateKeyEx(
                registry.HKEY_CURRENT_USER,
                WINDOWS_RUN_KEY,
                0,
                registry.KEY_SET_VALUE,
            )
            try:
                registry.SetValueEx(
                    key,
                    WINDOWS_VALUE_NAME,
                    0,
                    registry.REG_SZ,
                    command,
                )
            finally:
                registry.CloseKey(key)
        except OSError as exc:
            raise LoginStartupError(f"Could not enable Windows login startup: {exc}") from exc
        return

    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER,
            WINDOWS_RUN_KEY,
            0,
            registry.KEY_SET_VALUE,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LoginStartupError(f"Could not open the Windows login-startup registry key: {exc}") from exc
    try:
        try:
            registry.DeleteValue(key, WINDOWS_VALUE_NAME)
        except FileNotFoundError:
            pass
    except OSError as exc:
        raise LoginStartupError(f"Could not disable Windows login startup: {exc}") from exc
    finally:
        registry.CloseKey(key)


def login_startup_enabled(
    *,
    command: Sequence[str] | None = None,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    registry: Any | None = None,
) -> bool:
    """Return whether the current user's exact startup command is registered."""

    arguments = _validated_arguments(command)
    system = platform_name or platform.system()
    if system == "Windows":
        return _windows_login_startup_enabled(arguments, registry=registry)

    environment = os.environ if environ is None else environ
    home_path = Path.home() if home is None else Path(home)
    path = _registration_path(system, environ=environment, home=home_path)
    expected = _macos_launch_agent_bytes(arguments) if system == "Darwin" else _linux_autostart_bytes(arguments)
    return _read_registration(path) == expected


def set_login_startup(
    enabled: bool,
    *,
    command: Sequence[str] | None = None,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    registry: Any | None = None,
) -> None:
    """Enable or disable the current user's exact login-startup registration."""

    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")
    arguments = _validated_arguments(command)

    system = platform_name or platform.system()
    if system == "Windows":
        _set_windows_login_startup(enabled, arguments, registry=registry)
        return

    environment = os.environ if environ is None else environ
    home_path = Path.home() if home is None else Path(home)
    path = _registration_path(system, environ=environment, home=home_path)
    if not enabled:
        _remove_registration(path)
        return
    content = _macos_launch_agent_bytes(arguments) if system == "Darwin" else _linux_autostart_bytes(arguments)
    _write_registration(path, content)
