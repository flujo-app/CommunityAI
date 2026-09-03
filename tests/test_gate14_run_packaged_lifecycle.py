import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_run_packaged_lifecycle as entrypoint  # noqa: E402


class FakeActions:
    def __init__(self):
        self.closed = False

    def prepare(self, _config):
        raise AssertionError("mocked sequencer should own prepare")

    def calibrate(self, _config, _challenge):
        raise AssertionError("mocked sequencer should own calibrate")

    def cleanup(self, _config):
        raise AssertionError("mocked sequencer should own cleanup")

    def close(self):
        self.closed = True


def test_factory_selects_only_the_native_platform_adapters():
    assert entrypoint._factory("windows") is entrypoint.windows_transport.WindowsActionTransport
    assert entrypoint._factory("linux") is entrypoint.linux_transport.LinuxActionTransport
    with pytest.raises(entrypoint.Gate14LifecycleEntrypointError, match="platform"):
        entrypoint._factory("macos")


def test_run_from_config_binds_platform_and_closes_the_adapter(monkeypatch, tmp_path):
    config = SimpleNamespace(platform="linux")
    actions = FakeActions()
    expected = {
        "run_id": "gate14-a",
        "platform": "linux",
        "source_commit": "a" * 40,
    }
    seen = []

    monkeypatch.setattr(entrypoint.lifecycle, "load_config", lambda path: config)

    def run_lifecycle(observed_config, observed_actions):
        seen.append((observed_config, observed_actions))
        return expected

    monkeypatch.setattr(entrypoint.lifecycle, "run_lifecycle", run_lifecycle)

    assert (
        entrypoint.run_from_config(
            tmp_path / "gate14-lifecycle.json",
            action_factory=lambda observed: actions if observed is config else None,
            native_platform="linux",
        )
        == expected
    )
    assert seen == [(config, actions)]
    assert actions.closed is True


def test_run_from_config_rejects_cross_platform_execution_before_actions(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        entrypoint.lifecycle,
        "load_config",
        lambda path: SimpleNamespace(platform="windows"),
    )
    called = False

    def factory(_config):
        nonlocal called
        called = True
        return FakeActions()

    with pytest.raises(
        entrypoint.Gate14LifecycleEntrypointError,
        match="platform binding",
    ):
        entrypoint.run_from_config(
            tmp_path / "gate14-lifecycle.json",
            action_factory=factory,
            native_platform="linux",
        )
    assert called is False


def test_run_from_config_closes_actions_when_the_sequencer_fails(
    monkeypatch,
    tmp_path,
):
    config = SimpleNamespace(platform="linux")
    actions = FakeActions()
    monkeypatch.setattr(entrypoint.lifecycle, "load_config", lambda path: config)

    def fail(_config, _actions):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(entrypoint.lifecycle, "run_lifecycle", fail)
    with pytest.raises(RuntimeError, match="private diagnostic"):
        entrypoint.run_from_config(
            tmp_path / "gate14-lifecycle.json",
            action_factory=lambda _config: actions,
            native_platform="linux",
        )
    assert actions.closed is True


def test_invalid_adapter_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        entrypoint.lifecycle,
        "load_config",
        lambda path: SimpleNamespace(platform="linux"),
    )
    with pytest.raises(
        entrypoint.Gate14LifecycleEntrypointError,
        match="adapter",
    ):
        entrypoint.run_from_config(
            tmp_path / "gate14-lifecycle.json",
            action_factory=lambda _config: object(),
            native_platform="linux",
        )


def test_main_emits_only_a_bounded_failure_code(monkeypatch, capsys):
    def fail(_path):
        raise RuntimeError("secret private detail")

    monkeypatch.setattr(entrypoint, "run_from_config", fail)
    assert entrypoint.main(["--config", "/qualification/gate14/gate14-lifecycle.json"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "failure_code": "gate14_lifecycle_failed",
        "result": "failed",
        "schema_version": 1,
    }
