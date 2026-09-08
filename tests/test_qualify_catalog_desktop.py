import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

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
        assert replay.desktop_environment(args.visible_ui)["QT_QPA_PLATFORM"] == "windows"
