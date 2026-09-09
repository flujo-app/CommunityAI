import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qualify_login_startup_linux.py"
spec = importlib.util.spec_from_file_location("qualify_login_startup_linux", SCRIPT)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class Node:
    def __init__(self, name="", role="application", children=(), pid=0):
        self.name, self.role, self.nodes, self.pid = name, role, list(children), pid

    @property
    def childCount(self):
        return len(self.nodes)

    def getChildAtIndex(self, index):
        return self.nodes[index]

    def getRoleName(self):
        return self.role

    def get_process_id(self):
        return self.pid


def test_pid_selection_does_not_trust_application_title():
    wrong = Node("CommunityAI", pid=42)
    owned = Node("CommunityAI", pid=99)
    assert replay.select_application(Node(children=[wrong, owned]), 99) is owned
    with pytest.raises(RuntimeError, match="exactly one"):
        replay.select_application(Node(children=[wrong]), 99)


def test_checkbox_lookup_requires_unique_name_and_role():
    title = Node(replay.CHECKBOX_NAME, "label")
    checkbox = Node(replay.CHECKBOX_NAME, "check box")
    app = Node(children=[title, Node(children=[checkbox])])
    assert replay.find_named(app, replay.CHECKBOX_NAME, {"check box"}) is checkbox
    app.nodes.append(Node(replay.CHECKBOX_NAME, "check box"))
    with pytest.raises(RuntimeError, match="found 2"):
        replay.find_named(app, replay.CHECKBOX_NAME, {"check box"})


def test_malformed_or_cyclic_accessibility_tree_is_bounded(monkeypatch):
    monkeypatch.setattr(replay, "MAX_ACCESSIBLE_NODES", 4)
    cyclic = Node()
    cyclic.nodes.append(cyclic)
    with pytest.raises(RuntimeError, match="tree exceeds"):
        replay.find_named(cyclic, "missing", {"check box"})


@pytest.mark.parametrize("display", [None, "", ":0", ":0.0", "localhost:10.0", "host:99", ":99; command"])
def test_inherited_or_nonlocal_display_is_refused(display):
    with pytest.raises(RuntimeError, match="private numbered"):
        replay.private_display(display)


def test_private_xvfb_display_format():
    assert replay.private_display(":99") == ":99"


def test_qt_controls_can_expose_both_press_and_toggle_without_ambiguity():
    class Action:
        nActions = 3
        selected = []

        def getName(self, index):
            return ["Toggle", "Press", "SetFocus"][index]

        def doAction(self, index):
            self.selected.append(index)
            return True

    action = Action()
    control = type("Control", (), {"queryAction": lambda self: action})()
    assert replay.invoke_action(control, "Press") == "Press"
    assert replay.invoke_action(control, "Toggle") == "Toggle"
    assert action.selected == [1, 0]
    with pytest.raises(RuntimeError, match="Expected one"):
        replay.invoke_action(control, "UnreportedAction")
