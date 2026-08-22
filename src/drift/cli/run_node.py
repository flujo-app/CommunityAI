"""``drift node``: persistent, authenticated localhost gateway for manifested swarms."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from hivemind.utils.logging import get_logger, use_hivemind_log_handler

import drift
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.config import NODE_CONFIG_SCHEMA_VERSION, NodeConfig, NodeConfigError, NodeModelConfig
from drift.node.keys import load_or_create_api_key
from drift.node.loading import make_manifest_loader
from drift.node.model_manager import ModelDescriptor, ModelManager
from drift.utils.process_lifetime import tie_child_processes_to_this_process

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__name__)

DEFAULT_NODE_DATA_DIR = Path.home() / ".drift" / "node"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift node",
        description="Run a persistent local OpenAI gateway for manifested DRIFT swarms",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("model_manifest", nargs="?", help="Path to one exact ModelManifest v1 (legacy shorthand)")
    parser.add_argument("--config", type=Path, help="Path to a versioned multi-model node configuration")
    parser.add_argument("--initial_peers", nargs="+", help="Multiaddrs for the shorthand model to join via")
    parser.add_argument("--token", default=None, help="Hugging Face token for gated model artifacts")
    parser.add_argument("--cache_dir", default=None, help="Hugging Face cache directory")
    parser.add_argument(
        "--revocation_file",
        action="append",
        default=[],
        help="Signed identity rotation/revocation JSON to enforce (repeatable)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Interface for the local HTTP API")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--allow_network",
        action="store_true",
        help="Required acknowledgement before binding the authenticated API beyond loopback",
    )
    parser.add_argument(
        "--api_key",
        action="append",
        default=[],
        help="Bearer key accepted by both APIs (repeatable; otherwise a persistent key is generated)",
    )
    parser.add_argument("--data_dir", type=Path, default=DEFAULT_NODE_DATA_DIR)
    parser.add_argument(
        "--max_loaded_models",
        type=int,
        default=None,
        help="Override the maximum number of resident client runtimes",
    )
    parser.add_argument("--max_concurrent", type=int, default=1)
    parser.add_argument("--request_timeout", type=float, default=None)
    parser.add_argument("--max_retries", type=int, default=None)
    parser.add_argument("--default_max_tokens", type=int, default=512)
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if bool(args.model_manifest) == bool(args.config):
        parser.error("provide exactly one of model_manifest or --config")
    if args.model_manifest and not args.initial_peers:
        parser.error("model_manifest requires --initial_peers")
    if args.config and any(
        (
            args.initial_peers,
            args.cache_dir,
            args.revocation_file,
            args.request_timeout is not None,
            args.max_retries is not None,
        )
    ):
        parser.error(
            "--config supplies per-model peers, cache, revocations, timeout, and retries; "
            "do not combine their shorthand options"
        )
    if args.host.casefold() not in _LOOPBACK_HOSTS and not args.allow_network:
        parser.error("non-loopback --host requires explicit --allow_network")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.max_concurrent < 1:
        parser.error("--max_concurrent must be at least 1")
    if args.max_loaded_models is not None and args.max_loaded_models < 1:
        parser.error("--max_loaded_models must be at least 1")
    if args.request_timeout is not None and (not math.isfinite(args.request_timeout) or args.request_timeout <= 0):
        parser.error("--request_timeout must be positive")
    if args.max_retries is not None and args.max_retries < 1:
        parser.error("--max_retries must be at least 1")
    if args.default_max_tokens < 1:
        parser.error("--default_max_tokens must be at least 1")
    if any(not key for key in args.api_key):
        parser.error("--api_key values must not be empty")


def _load_node_config(args: argparse.Namespace) -> NodeConfig:
    if args.config:
        configured = NodeConfig.load(args.config)
        if args.max_loaded_models is None:
            return configured
        return NodeConfig(
            schema_version=configured.schema_version,
            max_loaded_models=args.max_loaded_models,
            models=configured.models,
        )

    manifest_path = Path(args.model_manifest).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else None
    return NodeConfig(
        schema_version=NODE_CONFIG_SCHEMA_VERSION,
        max_loaded_models=args.max_loaded_models or 1,
        models=(
            NodeModelConfig(
                manifest_path=manifest_path,
                initial_peers=tuple(args.initial_peers),
                cache_dir=cache_dir,
                revocation_files=tuple(Path(path).expanduser().resolve() for path in args.revocation_file),
                request_timeout=args.request_timeout if args.request_timeout is not None else 30.0,
                max_retries=args.max_retries if args.max_retries is not None else 3,
            ),
        ),
    )


def _build_model_manager(config: NodeConfig, *, token: str | None) -> tuple[ModelManager, tuple[ModelDescriptor, ...]]:
    manager = ModelManager(max_loaded_models=config.max_loaded_models)
    descriptors = []
    try:
        for model_config in config.models:
            manifest = ModelManifest.load(model_config.manifest_path)
            manifest.validate_runtime(drift.__version__)
            if manifest.runtime.adapter_profile != "none":
                raise ManifestError("Content-addressed adapter profiles are not executable in this release")
            descriptors.append(
                manager.register_manifest(
                    manifest,
                    make_manifest_loader(
                        manifest,
                        initial_peers=model_config.initial_peers,
                        token=token,
                        cache_dir=str(model_config.cache_dir) if model_config.cache_dir is not None else None,
                        revocation_files=tuple(str(path) for path in model_config.revocation_files),
                        request_timeout=model_config.request_timeout,
                        max_retries=model_config.max_retries,
                    ),
                )
            )
    except BaseException:
        manager.shutdown()
        raise
    return manager, tuple(descriptors)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    try:
        config = _load_node_config(args)
        manager, descriptors = _build_model_manager(config, token=args.token)
    except (NodeConfigError, ManifestError, ValueError) as exc:
        parser.error(str(exc))

    try:
        import uvicorn

        from drift.node.server import create_node_app
    except ImportError as exc:
        raise SystemExit(f"drift node requires the 'api' extra: pip install drift[api] ({exc})") from exc

    if args.api_key:
        api_keys = args.api_key
    else:
        key_path = args.data_dir / "local-api.key"
        key, created = load_or_create_api_key(key_path)
        api_keys = [key]
        if created:
            logger.info(f"Created the local API key in {key_path}; its value is not written to logs")
        else:
            logger.info(f"Using the local API key from {key_path}")

    # Arm this before a lazy request can create the model client's p2pd child.
    tie_child_processes_to_this_process()
    app = create_node_app(
        manager,
        api_keys=api_keys,
        host=args.host,
        port=args.port,
        max_concurrent=args.max_concurrent,
        default_max_tokens=args.default_max_tokens,
    )
    model_names = ", ".join(repr(descriptor.model_id) for descriptor in descriptors)
    logger.info(
        f"Local node knows {len(descriptors)} exact model(s) ({model_names}) at http://{args.host}:{args.port}/v1"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
