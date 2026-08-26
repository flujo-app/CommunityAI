"""``drift node``: persistent, authenticated localhost gateway for manifested swarms."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import torch
from hivemind.utils.logging import get_logger, use_hivemind_log_handler

import drift
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.config import NODE_CONFIG_SCHEMA_VERSION, NodeConfig, NodeConfigError, NodeModelConfig
from drift.node.discovery import CoverageTarget, ModelCoverageDiscovery
from drift.node.keys import ApiKeyStore, ApiKeyStoreError, load_or_create_api_key, load_or_create_control_key
from drift.node.loading import make_manifest_loader
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelNotFoundError
from drift.node.native_credentials import (
    DEFAULT_CREDENTIAL_ACCOUNT,
    DEFAULT_CREDENTIAL_SERVICE,
    NativeCredentialLocation,
    load_native_control_key,
)
from drift.node.policy_store import ContributionPolicyPersistenceError, ContributionPolicyStore
from drift.node.worker_supervisor import (
    NvidiaPowerMonitor,
    SystemBandwidthMonitor,
    WorkerLaunch,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)
from drift.utils.hardware import auto_detect_device, get_device_total_memory, is_accelerator, normalize_device
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
        help="Bearer key accepted by the OpenAI API (repeatable; otherwise a persistent client key is generated)",
    )
    parser.add_argument("--data_dir", type=Path, default=DEFAULT_NODE_DATA_DIR)
    parser.add_argument(
        "--control_key_path",
        type=Path,
        default=None,
        help="Private drift_control_ key file for the privileged control API (generated if missing)",
    )
    parser.add_argument(
        "--control_key_source",
        choices=("file", "native"),
        default="file",
        help="Read the privileged control key from a private file or the native OS credential store",
    )
    parser.add_argument("--credential_service", default=DEFAULT_CREDENTIAL_SERVICE, help=argparse.SUPPRESS)
    parser.add_argument("--credential_account", default=DEFAULT_CREDENTIAL_ACCOUNT, help=argparse.SUPPRESS)
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
    if args.control_key_source == "native" and args.control_key_path is not None:
        parser.error("--control_key_path cannot be combined with --control_key_source native")
    if not args.credential_service.strip() or not args.credential_account.strip():
        parser.error("native credential service and account must not be empty")


def _load_node_config(args: argparse.Namespace, *, persisted_config: NodeConfig | None = None) -> NodeConfig:
    if args.config:
        configured = persisted_config if persisted_config is not None else NodeConfig.load(args.config)
        if args.max_loaded_models is None:
            return configured
        return NodeConfig(
            schema_version=configured.schema_version,
            max_loaded_models=args.max_loaded_models,
            models=configured.models,
            workers=configured.workers,
            contribution_policy=configured.contribution_policy,
            discovery_update_period=configured.discovery_update_period,
            discovery_startup_timeout=configured.discovery_startup_timeout,
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


def _load_persisted_and_runtime_config(
    args: argparse.Namespace,
) -> tuple[NodeConfig | None, NodeConfig]:
    persisted = NodeConfig.load(args.config) if args.config is not None else None
    return persisted, _load_node_config(args, persisted_config=persisted)


def _build_model_manager(
    config: NodeConfig, *, token: str | None
) -> tuple[ModelManager, tuple[ModelDescriptor, ...], ModelCoverageDiscovery]:
    manager = ModelManager(max_loaded_models=config.max_loaded_models)
    descriptors = []
    try:
        configured_manifests = []
        for model_config in config.models:
            manifest = ModelManifest.load(model_config.manifest_path)
            manifest.validate_runtime(drift.__version__)
            if manifest.runtime.adapter_profile != "none":
                raise ManifestError("Content-addressed adapter profiles are not executable in this release")
            configured_manifests.append((model_config, manifest))

        discovery = ModelCoverageDiscovery(
            [
                CoverageTarget(
                    manifest=manifest,
                    initial_peers=model_config.initial_peers,
                    revocation_files=model_config.revocation_files,
                )
                for model_config, manifest in configured_manifests
            ],
            update_period=config.discovery_update_period,
            startup_timeout=config.discovery_startup_timeout,
        )
        manager.add_shutdown_callback(discovery.close)
        for model_config, manifest in configured_manifests:
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
                    route_health=discovery.observer(manifest.digest_id),
                )
            )
    except BaseException:
        manager.shutdown()
        raise
    return manager, tuple(descriptors), discovery


def _prepare_worker_supervisor_settings(
    config: NodeConfig, manager: ModelManager, *, token: str | None = None
) -> WorkerSupervisorSettings:
    policy = config.contribution_policy

    def model_key(descriptor: ModelDescriptor) -> str:
        return (descriptor.manifest_digest or descriptor.model_id).casefold()

    def resolve_policy_models(selectors: tuple[str, ...], field: str) -> set[str]:
        resolved = set()
        for selector in selectors:
            try:
                resolved.add(model_key(manager.resolve(selector)))
            except ModelNotFoundError as exc:
                raise NodeConfigError(f"contribution_policy.{field} selects {exc}") from exc
        return resolved

    allowed_models = resolve_policy_models(policy.allowed_models, "allowed_models")
    preferred_models = resolve_policy_models(policy.preferred_models, "preferred_models")
    denied_models = resolve_policy_models(policy.denied_models, "denied_models")
    if allowed_models.intersection(denied_models):
        raise NodeConfigError("contribution policy resolves the same model as both allowed and denied")
    if preferred_models.intersection(denied_models):
        raise NodeConfigError("contribution policy resolves the same model as both preferred and denied")
    if allowed_models and not preferred_models.issubset(allowed_models):
        raise NodeConfigError("contribution policy preferred models must also be allowed")
    if (policy.max_disk_space is None) != (policy.max_disk_bytes is None):
        raise NodeConfigError("contribution policy has an inconsistent max_disk_space value")
    if policy.sharing_enabled and policy.max_disk_bytes is None:
        raise NodeConfigError("contribution policy requires max_disk_space while sharing is enabled")
    if (policy.max_vram is None) != (policy.max_vram_bytes is None and policy.max_vram_fraction is None) or (
        policy.max_vram_bytes is not None and policy.max_vram_fraction is not None
    ):
        raise NodeConfigError("contribution policy has an inconsistent max_vram value")
    for field, value in (
        ("max_bandwidth_mbps", policy.max_bandwidth_mbps),
        ("max_power_watts", policy.max_power_watts),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
        ):
            raise NodeConfigError(f"contribution policy {field} must be a finite number > 0")

    manifested_models = {}
    for model_config in config.models:
        manifest = ModelManifest.load(model_config.manifest_path)
        manifested_models[manifest.digest_id] = (model_config, manifest)

    launches = []
    for worker in config.workers:
        try:
            descriptor = manager.resolve(worker.model)
        except ModelNotFoundError as exc:
            raise NodeConfigError(f"worker {worker.worker_id!r} selects {exc}") from exc
        model_config, manifest = manifested_models[descriptor.manifest_digest]
        if worker.num_blocks is not None and worker.num_blocks > manifest.model.num_blocks:
            raise NodeConfigError(
                f"worker {worker.worker_id!r} requests {worker.num_blocks} blocks from a "
                f"{manifest.model.num_blocks}-block model"
            )
        if worker.block_indices is not None and int(worker.block_indices.split(":")[1]) > manifest.model.num_blocks:
            raise NodeConfigError(
                f"worker {worker.worker_id!r} block range exceeds model size {manifest.model.num_blocks}"
            )
        if worker.public_ip is not None and worker.port is None:
            raise NodeConfigError(f"worker {worker.worker_id!r} public_ip requires port")

        resolved_model = model_key(descriptor)
        if not policy.sharing_enabled:
            policy_reason = "sharing is disabled by contribution policy"
        elif resolved_model in denied_models:
            policy_reason = f"model {descriptor.model_id!r} is denied by contribution policy"
        elif allowed_models and resolved_model not in allowed_models:
            policy_reason = f"model {descriptor.model_id!r} is not in the contribution allowlist"
        else:
            policy_reason = None
        policy_admitted = policy_reason is None

        if (worker.max_disk_space is None) != (worker.max_disk_bytes is None):
            raise NodeConfigError(f"worker {worker.worker_id!r} has an inconsistent max_disk_space value")
        disk_limits = [
            (worker.max_disk_bytes, worker.max_disk_space),
            (policy.max_disk_bytes, policy.max_disk_space),
        ]
        disk_limits = [(size, label) for size, label in disk_limits if size is not None]
        if disk_limits:
            effective_disk_bytes, effective_disk_space = min(disk_limits, key=lambda item: item[0])
        else:
            effective_disk_bytes = None
            effective_disk_space = None

        if (worker.max_vram is None) != (worker.max_vram_bytes is None and worker.max_vram_fraction is None) or (
            worker.max_vram_bytes is not None and worker.max_vram_fraction is not None
        ):
            raise NodeConfigError(f"worker {worker.worker_id!r} has an inconsistent max_vram value")
        for field, value in (
            ("max_bandwidth_mbps", worker.max_bandwidth_mbps),
            ("max_power_watts", worker.max_power_watts),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            ):
                raise NodeConfigError(f"worker {worker.worker_id!r} {field} must be a finite number > 0")
        effective_bandwidth_mbps = min(
            (value for value in (worker.max_bandwidth_mbps, policy.max_bandwidth_mbps) if value is not None),
            default=None,
        )
        effective_power_watts = min(
            (value for value in (worker.max_power_watts, policy.max_power_watts) if value is not None),
            default=None,
        )
        configured_device = normalize_device(torch.device(worker.device or auto_detect_device()))
        policy_vram_limit = (policy.max_vram_bytes, policy.max_vram_fraction)
        worker_vram_limit = (worker.max_vram_bytes, worker.max_vram_fraction)
        effective_vram_bytes = None
        policy_vram_bytes = None
        vram_device = None
        if is_accelerator(configured_device):
            if policy_admitted and policy_vram_limit == (None, None):
                raise NodeConfigError(
                    f"accelerator worker {worker.worker_id!r} requires a finite contribution max_vram"
                )
            if policy_vram_limit != (None, None):
                try:
                    total_vram = get_device_total_memory(configured_device)
                except Exception as exc:
                    raise NodeConfigError(
                        f"worker {worker.worker_id!r} cannot resolve max_vram for {configured_device}"
                    ) from exc

                def resolve_vram_limit(limit):
                    size, fraction = limit
                    return size if size is not None else math.floor(total_vram * fraction)

                policy_vram_bytes = min(total_vram, resolve_vram_limit(policy_vram_limit))
                effective_vram_bytes = policy_vram_bytes
                if worker_vram_limit != (None, None):
                    effective_vram_bytes = min(effective_vram_bytes, resolve_vram_limit(worker_vram_limit))
                vram_device = str(configured_device)

        if getattr(sys, "frozen", False):
            # desktop/launch_node.py dispatches this mode inside the packaged
            # sidecar; a frozen executable cannot be reinvoked with ``-m``.
            server_command = [sys.executable, "server"]
        else:
            server_command = [sys.executable, "-m", "drift.cli", "server"]
        command = [
            *server_command,
            manifest.source.repository,
            "--model_manifest",
            str(model_config.manifest_path),
            "--identity_path",
            str(worker.identity_path),
            "--initial_peers",
            *model_config.initial_peers,
            "--throughput",
            str(worker.throughput),
        ]
        if worker.num_blocks is not None:
            command.extend(("--num_blocks", str(worker.num_blocks)))
        else:
            command.extend(("--block_indices", worker.block_indices))
        if worker.device is not None:
            command.extend(("--device", worker.device))
        cache_dir = worker.cache_dir if worker.cache_dir is not None else model_config.cache_dir
        if cache_dir is not None:
            command.extend(("--cache_dir", str(cache_dir)))
        if effective_disk_space is not None:
            command.extend(("--max_disk_space", effective_disk_space))
        if effective_vram_bytes is not None:
            command.extend(("--max_device_memory", str(effective_vram_bytes)))
        if worker.port is not None:
            command.extend(("--port", str(worker.port)))
        if worker.public_ip is not None:
            command.extend(("--public_ip", worker.public_ip))
        for revocation_file in model_config.revocation_files:
            command.extend(("--revocation_file", str(revocation_file)))

        launches.append(
            WorkerLaunch(
                worker_id=worker.worker_id,
                model_id=descriptor.model_id,
                command=tuple(command),
                auto_start=worker.enabled,
                auto_restart=worker.auto_restart,
                restart_backoff=worker.restart_backoff,
                policy_admitted=policy_admitted,
                policy_reason=policy_reason,
                preferred=resolved_model in preferred_models,
                max_disk_bytes=effective_disk_bytes,
                max_vram_bytes=effective_vram_bytes,
                vram_device=vram_device,
                vram_pool_bytes=policy_vram_bytes,
                max_bandwidth_mbps=effective_bandwidth_mbps,
                max_power_watts=effective_power_watts,
                environment=(("HF_TOKEN", token),) if token is not None else (),
            )
        )
    bandwidth_monitor = (
        SystemBandwidthMonitor() if any(launch.max_bandwidth_mbps is not None for launch in launches) else None
    )
    power_monitors = {
        launch.worker_id.casefold(): NvidiaPowerMonitor([int(launch.vram_device.split(":", 1)[1])])
        for launch in launches
        if launch.max_power_watts is not None
        and launch.vram_device is not None
        and launch.vram_device.startswith("cuda:")
    }

    def worker_power_watts(worker_id: str) -> float | None:
        monitor = power_monitors.get(worker_id.casefold())
        return None if monitor is None else monitor()

    return WorkerSupervisorSettings(
        launches=tuple(launches),
        stop_timeout=policy.pause_timeout,
        schedule_allowed=None if policy.schedule is None else policy.schedule.allows,
        bandwidth_mbps=bandwidth_monitor,
        power_watts=worker_power_watts if any(launch.max_power_watts is not None for launch in launches) else None,
    )


def _build_worker_supervisor(
    config: NodeConfig, manager: ModelManager, *, token: str | None = None
) -> WorkerSupervisor:
    settings = _prepare_worker_supervisor_settings(config, manager, token=token)
    return WorkerSupervisor(
        settings.launches,
        stop_timeout=settings.stop_timeout,
        schedule_allowed=settings.schedule_allowed,
        bandwidth_mbps=settings.bandwidth_mbps,
        power_watts=settings.power_watts,
    )


def _prepare_api_key_store(data_dir: Path, supplied_keys: list[str]) -> tuple[ApiKeyStore, Path | None, bool]:
    """Import explicit keys or bootstrap only an otherwise keyless store.

    Once a control client has created a replacement and revoked the bootstrap client
    key, the persistent managed-key set is authoritative across node restarts.
    """
    key_store = ApiKeyStore(data_dir / "api-keys.json")
    if supplied_keys:
        for index, key in enumerate(supplied_keys, start=1):
            key_store.ensure_key(key, label=f"command-line-{index}")
        return key_store, None, False
    if any(item["revoked_at"] is None for item in key_store.list()):
        return key_store, None, False

    key_path = data_dir / "local-api.key"
    key, created = load_or_create_api_key(key_path)
    key_store.ensure_key(key, label="bootstrap")
    return key_store, key_path, created


def _prepare_control_key(
    data_dir: Path,
    supplied_path: Path | None,
    *,
    source: str = "file",
    credential_service: str = DEFAULT_CREDENTIAL_SERVICE,
    credential_account: str = DEFAULT_CREDENTIAL_ACCOUNT,
) -> tuple[str, Path | None, bool]:
    """Load or atomically create the credential used only by `/control/v1/*`."""
    if source == "native":
        if supplied_path is not None:
            raise ValueError("a control key path cannot be used with the native credential store")
        key = load_native_control_key(NativeCredentialLocation(credential_service, credential_account))
        return key, None, False
    if source != "file":
        raise ValueError(f"unsupported control key source {source!r}")
    key_path = supplied_path if supplied_path is not None else data_dir / "control-api.key"
    key, created = load_or_create_control_key(key_path)
    return key, key_path, created


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    try:
        persisted_config, config = _load_persisted_and_runtime_config(args)
        manager, descriptors, discovery = _build_model_manager(config, token=args.token)
        worker_supervisor = _build_worker_supervisor(config, manager, token=args.token)
        policy_store = (
            None
            if args.config is None
            else ContributionPolicyStore(
                args.config,
                worker_supervisor,
                lambda candidate: _prepare_worker_supervisor_settings(candidate, manager, token=args.token),
                expected_config=persisted_config,
            )
        )
    except (ContributionPolicyPersistenceError, NodeConfigError, ManifestError, ValueError) as exc:
        parser.error(str(exc))

    try:
        import uvicorn

        from drift.node.server import create_node_app
    except ImportError as exc:
        raise SystemExit(f"drift node requires the 'api' extra: pip install drift[api] ({exc})") from exc

    try:
        key_store, key_path, created = _prepare_api_key_store(args.data_dir, args.api_key)
        control_key, control_key_path, control_key_created = _prepare_control_key(
            args.data_dir,
            args.control_key_path,
            source=args.control_key_source,
            credential_service=args.credential_service,
            credential_account=args.credential_account,
        )
        if key_path is not None:
            if created:
                logger.info(f"Created the local API key in {key_path}; its value is not written to logs")
            else:
                logger.info(f"Using the local API key from {key_path}")
        elif not args.api_key:
            logger.info(f"Using the active managed API keys in {key_store.path}")
        if control_key_path is None:
            logger.info(
                f"Using the privileged local control key from native account {args.credential_account!r}; "
                "its value is not written to logs"
            )
        elif control_key_created:
            logger.info(
                f"Created the privileged local control key in {control_key_path}; its value is not written to logs"
            )
        else:
            logger.info(f"Using the privileged local control key from {control_key_path}")
    except (ApiKeyStoreError, OSError, ValueError) as exc:
        parser.error(str(exc))

    # Arm this before a lazy request can create the model client's p2pd child.
    tie_child_processes_to_this_process()
    app = create_node_app(
        manager,
        api_key_store=key_store,
        control_keys=[control_key],
        host=args.host,
        port=args.port,
        max_concurrent=args.max_concurrent,
        default_max_tokens=args.default_max_tokens,
        worker_supervisor=worker_supervisor,
        contribution_policy=config.contribution_policy,
        contribution_policy_store=policy_store,
    )
    model_names = ", ".join(repr(descriptor.model_id) for descriptor in descriptors)
    logger.info(
        f"Local node knows {len(descriptors)} exact model(s) ({model_names}) at http://{args.host}:{args.port}/v1"
    )
    discovery.start()
    worker_supervisor.start_service()
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        worker_supervisor.shutdown()
        manager.shutdown()


if __name__ == "__main__":
    main()
