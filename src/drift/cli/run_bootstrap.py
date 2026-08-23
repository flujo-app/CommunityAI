"""Provision a first-install node config from a threshold-signed model catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from drift.node.catalog_bootstrap import CatalogBootstrapError, bootstrap_node_from_catalog

DEFAULT_NODE_DATA_DIR = Path.home() / ".drift" / "node"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift bootstrap",
        description="Install a verified signed catalog as a first-run node configuration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("bootstrap_config", type=Path, help="Trusted release bootstrap JSON")
    parser.add_argument("--data_dir", type=Path, default=DEFAULT_NODE_DATA_DIR)
    parser.add_argument("--node_config", type=Path, help="Generated NodeConfig v1 path")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    config_path = (args.node_config or data_dir / "node-config.json").expanduser().resolve()
    try:
        result = bootstrap_node_from_catalog(args.bootstrap_config, data_dir=data_dir, config_path=config_path)
    except CatalogBootstrapError as exc:
        parser.error(str(exc))
    print(json.dumps(result.to_dict(), sort_keys=True))
