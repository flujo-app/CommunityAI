import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qualify_catalog_desktop.py"
sys.path.insert(0, str(SCRIPT.parents[1] / "desktop" / "src"))
spec = importlib.util.spec_from_file_location("qualify_catalog_desktop", SCRIPT)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def test_default_replay_cannot_inherit_a_visible_qt_platform():
    args = replay.build_parser().parse_args(["--desktop", "CommunityAI.exe", "--output", "new-state"])
    assert args.visible_ui is False
    with patch.dict(replay.os.environ, {"QT_QPA_PLATFORM": "windows", "KEEP_TEST_ENV": "value"}):
        environment = replay.desktop_environment(args.visible_ui)
        assert environment["QT_QPA_PLATFORM"] == "offscreen"
        assert environment["KEEP_TEST_ENV"] == "value"
        assert replay.os.environ["QT_QPA_PLATFORM"] == "windows"


def test_native_windows_require_explicit_visible_qualification_option():
    args = replay.build_parser().parse_args(["--desktop", "CommunityAI.exe", "--output", "new-state", "--visible-ui"])
    with patch.dict(replay.os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
        assert replay.desktop_environment(args.visible_ui, system="Windows")["QT_QPA_PLATFORM"] == "windows"


def test_linux_replay_stays_offscreen_and_selects_its_packaged_node():
    assert replay.desktop_environment(False, system="Linux")["QT_QPA_PLATFORM"] == "offscreen"
    assert replay.node_executable(Path("bundle/CommunityAI"), "Linux") == Path("bundle/node/CommunityAI-Node")
    assert replay.node_executable(Path("bundle/CommunityAI.exe"), "Windows") == Path("bundle/node/CommunityAI-Node.exe")
    with pytest.raises(ValueError, match="offscreen"):
        replay.desktop_environment(True, system="Linux")


def test_linux_replay_requires_ordinary_uid_without_windows_calls(monkeypatch):
    monkeypatch.setattr(replay.platform, "system", lambda: "Linux")
    monkeypatch.setattr(replay.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(replay.ctypes, "windll", None, raising=False)
    assert replay.qualification_platform() == "Linux"
    assert replay.visible_window(1234) is False
    monkeypatch.setattr(replay.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="ordinary Linux user"):
        replay.qualification_platform()


def test_windows_replay_still_refuses_elevation(monkeypatch):
    monkeypatch.setattr(replay.platform, "system", lambda: "Windows")
    admin = SimpleNamespace(IsUserAnAdmin=lambda: True)
    monkeypatch.setattr(replay.ctypes, "windll", SimpleNamespace(shell32=admin), raising=False)
    with pytest.raises(RuntimeError, match="non-elevated Windows"):
        replay.qualification_platform()
    admin.IsUserAnAdmin = lambda: False
    assert replay.qualification_platform() == "Windows"
