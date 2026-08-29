import os
from pathlib import Path

import pytest

from drift.model_manifest import ModelManifest
from scripts import public_route_node as node

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = {
    "qwen3.5-2b": (
        REPOSITORY_ROOT / "manifests" / "candidates" / "qwen3.5-2b-bfloat16-eager.json",
        31337,
    ),
    "gemma-4-e2b": (
        REPOSITORY_ROOT / "manifests" / "candidates" / "gemma-4-e2b-it-bfloat16-eager.json",
        31338,
    ),
}


def _peer(letter: str = "S") -> str:
    return "Qm" + letter * 44


def _configure(monkeypatch, candidate):
    manifest_path, _port = CANDIDATES[candidate]
    monkeypatch.setattr(node, "_DEFAULT_MANIFEST_PATH", os.fspath(manifest_path))
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE", candidate)
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_IPV4", "8.8.8.8")
    monkeypatch.setenv(
        "COMMUNITYAI_PUBLIC_ROUTE_INITIAL_PEER",
        f"/dns4/bootstrap.communityai.example/tcp/31337/p2p/{_peer()}",
    )
    return ModelManifest.load(manifest_path)


@pytest.mark.parametrize("candidate", tuple(CANDIDATES))
def test_worker_is_complete_cuda_manifest_route_with_bounded_health(monkeypatch, candidate):
    manifest = _configure(monkeypatch, candidate)
    _manifest_path, port = CANDIDATES[candidate]

    command = node.build_worker_args()

    assert command[:3] == ["drift", "server", manifest.source.repository]
    assert command[command.index("--block_indices") + 1] == f"0:{manifest.model.num_blocks}"
    assert command[command.index("--device") + 1] == "cuda"
    expected_memory = "7GiB" if candidate == "qwen3.5-2b" else "15GiB"
    assert command[command.index("--max_device_memory") + 1] == expected_memory
    assert command[command.index("--torch_dtype") + 1] == manifest.runtime.dtype
    assert command[command.index("--attn_implementation") + 1] == manifest.runtime.attention_implementation
    assert command[command.index("--quant_type") + 1] == manifest.runtime.quantization
    assert command[command.index("--host_maddrs") + 1] == f"/ip4/0.0.0.0/tcp/{port}"
    assert command[command.index("--announce_maddrs") + 1] == f"/ip4/8.8.8.8/tcp/{port}"
    assert command[command.index("--health_state_path") + 1] == "/run/communityai/health.json"
    assert command[command.index("--identity_path") + 1] == "/run/communityai/identity.key"
    assert command[command.index("--cache_dir") + 1] == "/cache/model"
    assert command[command.index("--max_batch_size") + 1] == "1"
    assert command[command.index("--attn_cache_tokens") + 1] == "512"
    assert "--allow_training_rpcs" not in command


def test_worker_freezes_public_admission_and_network_limits(monkeypatch):
    _configure(monkeypatch, "qwen3.5-2b")

    command = node.build_worker_args()

    expected = {
        "--num_handlers": "1",
        "--admission_max_active_sessions": "8",
        "--admission_max_active_sessions_per_peer": "1",
        "--admission_global_session_rate": "2.0",
        "--admission_global_session_burst": "4",
        "--admission_peer_session_rate": "0.25",
        "--admission_peer_session_burst": "1",
        "--admission_max_tracked_peers": "512",
        "--admission_tracked_peer_ttl": "300",
        "--admission_max_pending_pushes": "4",
        "--max_chunk_size_bytes": str(16 * 1024 * 1024),
        "--request_timeout": "60",
        "--session_timeout": "60",
        "--step_timeout": "30",
    }
    for option, value in expected.items():
        assert command.count(option) == 1
        assert command[command.index(option) + 1] == value
    assert "--no_auto_relay" in command


@pytest.mark.parametrize(
    "address",
    [
        "",
        "not-an-ip",
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "192.0.2.1",
        "224.0.0.1",
        "::1",
        "8.8.8.8\nprivate",
    ],
)
def test_worker_rejects_non_global_or_malformed_public_address(monkeypatch, address):
    _configure(monkeypatch, "qwen3.5-2b")
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_IPV4", address)

    with pytest.raises(node.PublicRouteConfigurationError, match="IPv4|global|bounded"):
        node.build_worker_args()


@pytest.mark.parametrize(
    "peer",
    [
        "",
        "not-a-multiaddr",
        "/ip4/8.8.8.8/tcp/31337",
        "/ip4/8.8.8.8/tcp/31337/p2p/not!base58",
        f"/ip4/8.8.8.8/tcp/31337/p2p/{_peer()}/p2p/{_peer('T')}",
        f"/ip4/8.8.8.8/tcp/31337/p2p/{_peer()}\nsecret",
        f"/ip4/8.8.8.8/tcp/31337/p2p/{_peer()} extra",
        "x" * 2049,
    ],
)
def test_worker_rejects_unauthenticated_or_unbounded_bootstrap(monkeypatch, peer):
    _configure(monkeypatch, "qwen3.5-2b")
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_INITIAL_PEER", peer)

    with pytest.raises(node.PublicRouteConfigurationError, match="bootstrap peer|bounded"):
        node.build_worker_args()


def test_worker_rejects_candidate_manifest_mismatch(monkeypatch):
    manifest_path, _port = CANDIDATES["gemma-4-e2b"]
    monkeypatch.setattr(node, "_DEFAULT_MANIFEST_PATH", os.fspath(manifest_path))
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE", "qwen3.5-2b")
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_IPV4", "8.8.8.8")
    monkeypatch.setenv(
        "COMMUNITYAI_PUBLIC_ROUTE_INITIAL_PEER",
        f"/ip4/8.8.8.8/tcp/31337/p2p/{_peer()}",
    )

    with pytest.raises(node.PublicRouteConfigurationError, match="immutable candidate"):
        node.build_worker_args()


@pytest.mark.parametrize("candidate", ["", "other", "qwen3.5-2b\nsecret"])
def test_worker_rejects_unbound_candidate_without_leaking_input(monkeypatch, candidate):
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE", candidate)

    with pytest.raises(node.PublicRouteConfigurationError) as captured:
        node.build_worker_args()

    if candidate:
        assert candidate not in str(captured.value)


def test_worker_manifest_error_does_not_expose_path_or_exception(monkeypatch, tmp_path):
    private_path = tmp_path / "private-owner-manifest.json"
    monkeypatch.setattr(node, "_DEFAULT_MANIFEST_PATH", os.fspath(private_path))
    monkeypatch.setenv("COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE", "qwen3.5-2b")

    with pytest.raises(node.PublicRouteConfigurationError, match="invalid or incompatible") as captured:
        node.build_worker_args()

    assert os.fspath(private_path) not in str(captured.value)


def test_main_execs_shell_free_worker_argv(monkeypatch):
    _configure(monkeypatch, "qwen3.5-2b")
    calls = []

    def fake_execvp(executable, command):
        calls.append((executable, command))
        raise RuntimeError("exec boundary")

    monkeypatch.setattr(node.os, "execvp", fake_execvp)

    with pytest.raises(RuntimeError, match="exec boundary"):
        node.main([])

    assert calls
    assert calls[0][0] == "drift"
    assert calls[0][1] == node.build_worker_args()


def test_main_rejects_all_arguments_before_exec(monkeypatch):
    monkeypatch.setattr(node.os, "execvp", lambda *args: pytest.fail("must not execute"))

    with pytest.raises(node.PublicRouteConfigurationError, match="unexpected"):
        node.main(["identity"])
