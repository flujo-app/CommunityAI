"""Provision a first-install node config from a threshold-signed model catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from drift.node.catalog_bootstrap import (
    CatalogBootstrapConfig,
    CatalogBootstrapError,
    CatalogBootstrapInstaller,
    bootstrap_node_from_catalog,
)
from drift.node.config import NodeConfig

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
    parser.add_argument(
        "--refresh", action="store_true", help="Authenticate a newer catalog while preserving existing user settings"
    )
    parser.add_argument(
        "--refresh_if_needed",
        action="store_true",
        help="Migrate a legacy installation to authenticated periodic refresh",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    config_path = (args.node_config or data_dir / "node-config.json").expanduser().resolve()
    try:
        if args.refresh or args.refresh_if_needed:
            installer = CatalogBootstrapInstaller(
                CatalogBootstrapConfig.load(args.bootstrap_config), data_dir=data_dir, config_path=config_path
            )
            existing = NodeConfig.load(config_path)
            needs_refresh = existing.catalog_path is None
            if existing.catalog_bootstrap_path is not None:
                installed = CatalogBootstrapConfig.load(existing.catalog_bootstrap_path)
                # Only a locally installed application bundle can supply a new
                # root. A catalog fetched from the network cannot change it.
                needs_refresh = (
                    installer.bootstrap.trust_root != installed.trust_root
                    and installer.bootstrap.permits_replacement_of(installed)
                )
            result = installer.refresh() if args.refresh or needs_refresh else installer._existing_result()
        else:
            result = bootstrap_node_from_catalog(args.bootstrap_config, data_dir=data_dir, config_path=config_path)
    except CatalogBootstrapError as exc:
        parser.error(str(exc))
    print(json.dumps(result.to_dict(), sort_keys=True))
