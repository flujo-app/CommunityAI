"""Isolated signed-channel checks with in-memory consumer/status adapters."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gate16_catalog_channel as channel

from drift.model_catalog import SignedModelCatalog
from drift.node.catalog_bootstrap import CatalogBootstrapInstaller

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "public-alpha/catalog-qwen-v2"
PEER = "/ip4/8.8.8.8/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo"


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    original = SignedModelCatalog.from_json((RELEASE / "catalog.signed.json").read_text())
    now = original.signed.issued_at_ms / 1000 + 1
    monkeypatch.setattr(channel.time, "time", lambda: now)
    args = SimpleNamespace(
        release=RELEASE,
        base_url="https://canary.example.com/gate16/",
        initial_peer=PEER,
        run_id="test-channel",
        output=tmp_path / "bundle",
    )
    result = channel.prepare(args, now=now)
    assert result["published"] is False
    return args.output


def test_prepare_uses_separate_root_presigns_all_phases_and_never_retains_key(bundle):
    index, bootstrap = channel.load_bundle(bundle)
    production = json.loads((RELEASE / "catalog-bootstrap.json").read_text())
    assert bootstrap.trust_root.catalog_id.startswith("communityai-canary-")
    assert bootstrap.trust_root.to_dict() != production["trust_root"]
    assert [index["phases"][phase]["sequence"] for phase in channel.PHASES] == [1, 2, 3]
    assert index["private_signing_key_retained"] is False
    assert not list(bundle.rglob("*.pem")) and not list(bundle.rglob("*.key"))
    config = json.loads((bundle / "private-node/node-config.json").read_text())
    assert config["inference_mode"] == "local_only"
    assert config["contribution_policy"]["sharing_enabled"] is False
    assert not list((bundle / "private-node").rglob("*.safetensors"))


def test_phase_advance_rejects_skip_replay_and_tamper(bundle):
    with pytest.raises(channel.ChannelError, match="exactly_once"):
        channel.advance(SimpleNamespace(bundle=bundle, phase="restore"))
    assert channel.advance(SimpleNamespace(bundle=bundle, phase="withdrawal"))["sequence"] == 2
    with pytest.raises(channel.ChannelError, match="exactly_once"):
        channel.advance(SimpleNamespace(bundle=bundle, phase="withdrawal"))
    active = (bundle / "channel/catalog.signed.json").read_bytes()
    path = bundle / "phases/restore.signed.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(channel.ChannelError, match="phase_digest_mismatch"):
        channel.advance(SimpleNamespace(bundle=bundle, phase="restore"))
    assert (bundle / "channel/catalog.signed.json").read_bytes() == active


def status(started_at, config_path):
    return {
        "status": "running",
        "started_at": started_at,
        "inference_mode": "local_only",
        "contribution": {
            "policy": {
                "config_revision": "sha256:" + channel.sha(config_path.read_bytes()),
                "policy": {"sharing_enabled": False},
            }
        },
        "workers": [{"state": "paused"}],
        "runtime_budget": {"resident_models": 0},
    }


def observe_args(bundle, tmp_path, phase, previous=None):
    return SimpleNamespace(
        bundle=bundle,
        phase=phase,
        previous=previous,
        node_config=bundle / "private-node/node-config.json",
        node_url="http://127.0.0.1:18116",
        credential_service="unused-test",
        credential_account="unused-test",
        timeout=10,
        output=tmp_path / ("observe-" + phase),
    )


def refresh(bundle):
    _, bootstrap = channel.load_bundle(bundle)

    def fetch(url, _limit):
        suffix = url.removeprefix("https://canary.example.com/gate16/")
        return (bundle / "channel" / suffix).read_text()

    return CatalogBootstrapInstaller(
        bootstrap,
        data_dir=bundle / "private-node",
        config_path=bundle / "private-node/node-config.json",
        fetch_text=fetch,
    ).refresh()


def test_observer_requires_restart_and_preserves_preferences_through_forward_restore(bundle, tmp_path):
    baseline_args = observe_args(bundle, tmp_path, "baseline")
    baseline = channel.observe(baseline_args, get_status=lambda: status(100, baseline_args.node_config))
    assert baseline["result"] == "passed"
    previous = baseline_args.output / "result.json"
    for phase, started_at in (("withdrawal", 101), ("restore", 102)):
        channel.advance(SimpleNamespace(bundle=bundle, phase=phase))
        assert refresh(bundle).created
        samples = iter(
            (
                httpx.ConnectError("restart in progress"),
                status(started_at - 1, baseline_args.node_config),
                status(started_at, baseline_args.node_config),
            )
        )

        def read_status():
            value = next(samples)
            if isinstance(value, Exception):
                raise value
            return value

        args = observe_args(bundle, tmp_path, phase, previous)
        result = channel.observe(args, get_status=read_status, sleep=lambda _: None)
        assert result["result"] == "passed" and result["node_restarted"] is True
        assert result["local_preferences_sha256"] == baseline["local_preferences_sha256"]
        assert result["cleanup"] == {"created_processes": 0, "created_credentials": 0, "owned_http_client_closed": True}
        previous = args.output / "result.json"


def test_file_update_without_active_node_restart_cannot_pass(bundle, tmp_path):
    args = observe_args(bundle, tmp_path, "baseline")
    assert channel.observe(args, get_status=lambda: status(100, args.node_config))["result"] == "passed"
    channel.advance(SimpleNamespace(bundle=bundle, phase="withdrawal"))
    assert refresh(bundle).created
    clock = iter(range(30))
    waiting = observe_args(bundle, tmp_path, "withdrawal", args.output / "result.json")
    result = channel.observe(
        waiting, get_status=lambda: status(100, args.node_config), monotonic=lambda: next(clock), sleep=lambda _: None
    )
    assert result["result"] == "failed" and result["error_code"] == "catalog_observation_deadline"


@pytest.mark.parametrize("problem", ["stale_active_revision", "stopping"])
def test_saved_new_catalog_with_unrelated_restart_or_stopping_node_cannot_pass(bundle, tmp_path, problem):
    args = observe_args(bundle, tmp_path, "baseline")
    before = status(100, args.node_config)
    assert channel.observe(args, get_status=lambda: before)["result"] == "passed"
    channel.advance(SimpleNamespace(bundle=bundle, phase="withdrawal"))
    assert refresh(bundle).created
    sample = status(101, args.node_config)
    if problem == "stale_active_revision":
        sample["contribution"]["policy"]["config_revision"] = before["contribution"]["policy"]["config_revision"]
    else:
        sample["status"] = "stopping"
    clock = iter(range(30))
    waiting = observe_args(bundle, tmp_path, "withdrawal", args.output / "result.json")
    result = channel.observe(waiting, get_status=lambda: sample, monotonic=lambda: next(clock), sleep=lambda _: None)
    assert result["result"] == "failed" and result["error_code"] == "catalog_observation_deadline"
    assert json.loads((waiting.output / "result.json").read_text())["result"] == "failed"


def test_http_cleanup_failure_preserves_failed_result_json(bundle, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "desktop/src"))
    from communityai_desktop.credentials import NativeCredentialStore

    args = observe_args(bundle, tmp_path, "baseline")
    monkeypatch.setattr(NativeCredentialStore, "get", lambda self: "never-transmitted-test-value")

    class Client:
        def __init__(self, **kwargs):
            pass

        def get(self, path):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: status(100, args.node_config))

        def close(self):
            raise OSError("simulated HTTP cleanup failure")

    monkeypatch.setattr(channel.httpx, "Client", Client)
    result = channel.observe(args)
    assert result["result"] == "failed" and result["cleanup_error_type"] == "OSError"
    assert result["cleanup"]["owned_http_client_closed"] is False
    assert json.loads((args.output / "result.json").read_text())["result"] == "failed"
