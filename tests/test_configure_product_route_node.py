import json
import sys
from pathlib import Path

import pytest

from drift.node.config import NodeConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from configure_product_route_node import ProductRouteConfigError, configure_product_route_node  # noqa: E402

BOOTSTRAP_PEER = "/dns4/bootstrap.communityai.flujo.com.co/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo"


def _node_config(tmp_path: Path) -> Path:
    manifests = sorted((ROOT / "public-alpha" / "catalog-v1" / "manifests").glob("*.json"))
    source = {
        "schema_version": 1,
        "max_loaded_models": 1,
        "models": [
            {
                "manifest": str(manifest),
                "initial_peers": [BOOTSTRAP_PEER],
                "cache_dir": str(tmp_path / "model-cache" / manifest.stem),
            }
            for manifest in manifests
        ],
        "auto_model_priority": [
            "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
            "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        ],
        "workers": [
            {
                "id": "automatic",
                "model": "auto",
                "identity_path": str(tmp_path / "automatic.key"),
                "num_blocks": 1,
                "enabled": True,
            }
        ],
    }
    path = tmp_path / "node-config.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "role,blocks,port,vram",
    (("primary", 24, 31337, "7GiB"), ("standby", 35, 31338, "15GiB")),
)
def test_configures_full_route_through_automatic_worker(tmp_path, role, blocks, port, vram):
    path = _node_config(tmp_path)
    report = configure_product_route_node(
        path,
        role=role,
        public_ip="203.0.113.20",
        cache_root=tmp_path / "worker-cache",
    )

    source = json.loads(path.read_text(encoding="utf-8"))
    config = NodeConfig.load(path)
    worker = config.workers[0]
    assert report["automatic_placement"] is True
    assert report["model_artifacts_embedded_in_runtime"] is False
    assert worker.model == "auto"
    assert worker.num_blocks == blocks
    assert worker.port == port
    assert worker.public_ip == "203.0.113.20"
    assert worker.max_vram == vram
    assert source["contribution_policy"]["sharing_enabled"] is True
    assert len(source["contribution_policy"]["allowed_models"]) == 1


def test_rejects_wrong_candidate_set(tmp_path):
    path = _node_config(tmp_path)
    source = json.loads(path.read_text(encoding="utf-8"))
    source["models"] = source["models"][:1]
    path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ProductRouteConfigError, match="exactly two"):
        configure_product_route_node(
            path,
            role="primary",
            public_ip="203.0.113.20",
            cache_root=tmp_path / "worker-cache",
        )


def test_rejects_non_ipv4_public_address(tmp_path):
    path = _node_config(tmp_path)
    with pytest.raises(ProductRouteConfigError, match="IPv4"):
        configure_product_route_node(
            path,
            role="primary",
            public_ip="not-an-address",
            cache_root=tmp_path / "worker-cache",
        )
