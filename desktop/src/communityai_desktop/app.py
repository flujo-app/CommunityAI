"""Command-line and packaged entry point for the CommunityAI desktop."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from communityai_desktop.acceptance import fake_node, run_self_test
from communityai_desktop.client import NodeClient, NodeClientError, normalize_loopback_url
from communityai_desktop.controller import DesktopController
from communityai_desktop.credentials import (
    DEFAULT_CREDENTIAL_ACCOUNT,
    DEFAULT_CREDENTIAL_SERVICE,
    CredentialError,
    NativeCredentialStore,
)
from communityai_desktop.lifecycle import (
    DEFAULT_NODE_CONFIG_PATH,
    DEFAULT_NODE_DATA_DIR,
    NodeLifecycleError,
    NodeLifecycleSupervisor,
    default_bootstrap_config_path,
)
from communityai_desktop.profiles import VOLUNTEER_PROFILE, VolunteerProfile
from communityai_desktop.release import RELEASE_VERSION
from communityai_desktop.startup import LOGIN_STARTUP_FLAG, SingleInstanceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CommunityAI desktop", allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {RELEASE_VERSION}")
    parser.add_argument("--profile", choices=("standard", VOLUNTEER_PROFILE), default="standard")
    parser.add_argument("--node-url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--credential-service", default=DEFAULT_CREDENTIAL_SERVICE, help=argparse.SUPPRESS)
    parser.add_argument("--credential-account", default=DEFAULT_CREDENTIAL_ACCOUNT, help=argparse.SUPPRESS)
    parser.add_argument("--node-config", type=Path, default=DEFAULT_NODE_CONFIG_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--node-data-dir", type=Path, default=DEFAULT_NODE_DATA_DIR, help=argparse.SUPPRESS)
    parser.add_argument(
        "--bootstrap-config", type=Path, default=default_bootstrap_config_path(), help=argparse.SUPPRESS
    )
    parser.add_argument("--no-manage-node", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(LOGIN_STARTUP_FLAG, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--capture-page", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--gate13-ui-evidence", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--gate13-ui-screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--resource-ui-evidence", type=Path, help=argparse.SUPPRESS)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--store-control-key", action="store_true")
    action.add_argument("--delete-control-key", action="store_true")
    action.add_argument("--check-runtime", action="store_true")
    action.add_argument("--self-test", action="store_true")
    action.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--onboarding-ui-self-test", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--capture-ui", type=Path, help=argparse.SUPPRESS)
    action.add_argument("--probe-only", action="store_true", help=argparse.SUPPRESS)
    action.add_argument(
        "--prepare-update", action="store_true", help="Stop this user's desktop and owned node for installation"
    )
    action.add_argument("--gate13-ui-playthrough", type=Path, help=argparse.SUPPRESS)
    action.add_argument("--resource-ui-playthrough", type=Path, help=argparse.SUPPRESS)
    return parser


def _credential_store(args: argparse.Namespace) -> NativeCredentialStore:
    return NativeCredentialStore(args.credential_service, args.credential_account)


def _write_json(value: Any) -> None:
    """Write diagnostics when attached to a console; windowed bundles have none."""
    if sys.stdout is not None:
        print(json.dumps(value, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None, *, forced_profile: str | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if forced_profile is not None:
        if any(option.split("=", 1)[0] == "--profile" for option in arguments):
            parser.error("This test launcher has a fixed profile")
        arguments = ["--profile", forced_profile, *arguments]
    args = parser.parse_args(arguments)
    profile = None
    if args.profile == VOLUNTEER_PROFILE:
        # Fixed identities prevent a test launcher accidentally adopting the regular app.
        protected_options = (
            "--node-url",
            "--node-config",
            "--node-data-dir",
            "--credential-service",
            "--credential-account",
            "--no-manage-node",
            "--started-at-login",
            "--bootstrap-config",
        )
        if any(option.split("=", 1)[0] in protected_options for option in arguments):
            parser.error(
                "The volunteer profile uses fixed local paths, credentials and port; overrides are not supported"
            )
        profile = VolunteerProfile.for_current_user()
        args.node_url = profile.node_url
        args.node_config = profile.config_path
        args.node_data_dir = profile.data_dir
        args.credential_service = profile.credential_service
        args.credential_account = profile.credential_account
    shell_profile_options = (
        {}
        if profile is None
        else {
            "application_name": profile.application_name,
            "instance_data_dir": profile.instance_dir,
            "allow_login_startup": False,
        }
    )
    anchored = profile is not None and sys.platform.startswith("linux")
    anchor_prepared = None
    if anchored:
        shell_profile_options.update(allow_instance_directory_creation=False, allow_maintenance_ack=False)
    if args.gate13_ui_playthrough is None:
        if args.gate13_ui_evidence is not None or args.gate13_ui_screenshot is not None:
            parser.error("Gate 13 evidence options require --gate13-ui-playthrough")
    elif args.gate13_ui_evidence is None:
        parser.error("--gate13-ui-playthrough requires --gate13-ui-evidence")
    if bool(args.resource_ui_playthrough) != bool(args.resource_ui_evidence):
        parser.error("Resource UI playthrough requires both plan and evidence paths")
    try:
        if anchored and (args.store_control_key or args.delete_control_key):
            raise NodeLifecycleError(
                "Linux test-profile credential changes require exclusive anchor recovery, which is not available yet"
            )
        if anchored and args.probe_only:
            raise NodeLifecycleError("Linux test-profile probe-only is unavailable without desktop instance ownership")
        if profile is not None and not args.prepare_update:
            if anchored:
                from communityai_desktop.anchor_lifecycle import LinuxAnchorLifecycle, prepare_anchored_profile

                anchor_prepared = prepare_anchored_profile(profile)
            else:
                profile.prepare()
        if args.prepare_update:
            if anchored:
                from communityai_desktop.anchor_lifecycle import MAINTENANCE_ERROR

                raise NodeLifecycleError(MAINTENANCE_ERROR)
            try:
                from communityai_desktop.maintenance import prepare_update

                return prepare_update(
                    **(
                        {"application_name": profile.application_name, "instance_data_dir": profile.instance_dir}
                        if profile is not None
                        else {}
                    )
                )
            except Exception as exc:
                # A windowed PyInstaller traceback dialog would hold the installer
                # indefinitely if, for example, the installed Qt runtime is broken.
                parser.exit(2, f"CommunityAI shutdown failed: {exc}\n")
        if args.self_test:
            _write_json(run_self_test())
            return 0
        if args.check_runtime:
            from communityai_desktop.pyside_shell import check_runtime

            _write_json(check_runtime())
            return 0
        if args.ui_self_test:
            from communityai_desktop.pyside_shell import run

            with fake_node() as (url, token):
                return int(
                    run(
                        DesktopController(NodeClient(url, token)),
                        auto_close_seconds=1.0,
                        single_instance=False,
                        **shell_profile_options,
                    )
                    or 0
                )
        if args.onboarding_ui_self_test:
            from communityai_desktop.pyside_shell import run

            def missing_credential():
                raise CredentialError("no local-node control credential is stored")

            return int(
                run(
                    connect=missing_credential,
                    auto_close_seconds=1.0,
                    single_instance=False,
                    **shell_profile_options,
                )
                or 0
            )
        if args.capture_ui:
            from communityai_desktop.pyside_shell import run

            with fake_node() as (url, token):
                return int(
                    run(
                        DesktopController(NodeClient(url, token)),
                        auto_close_seconds=1.2,
                        screenshot_path=args.capture_ui,
                        screenshot_page=args.capture_page,
                        single_instance=False,
                        **shell_profile_options,
                    )
                    or 0
                )
        if args.store_control_key:
            secret = getpass.getpass("Local node control credential: ")
            _credential_store(args).set(secret)
            print(f"Stored control credential for account {args.credential_account!r}")
            return 0
        if args.delete_control_key:
            deleted = _credential_store(args).delete()
            print("Deleted control credential" if deleted else "No stored control credential")
            return 0

        # Validate the destination before opening the credential store.
        node_url = normalize_loopback_url(args.node_url)
        credential_store = _credential_store(args)
        lifecycle = (
            None
            if args.no_manage_node
            else LinuxAnchorLifecycle(
                profile,
                credential_store,
                prepared=anchor_prepared,
                client_timeout=args.timeout,
            )
            if anchored
            else NodeLifecycleSupervisor(
                node_url,
                credential_store,
                config_path=args.node_config,
                data_dir=args.node_data_dir,
                bootstrap_config_path=args.bootstrap_config,
                client_timeout=args.timeout,
                **(
                    {
                        "environment_overrides": profile.child_environment(),
                        "validate_config": profile.validate_config,
                        "pause_sharing_on_start": True,
                        "local_inference_cpu_only": True,
                        "allow_external_node": False,
                    }
                    if profile is not None
                    else {}
                ),
            )
        )

        def connect() -> DesktopController:
            if lifecycle is not None:
                return DesktopController(lifecycle.ensure_client())
            token = credential_store.get_or_migrate(args.node_data_dir / "control-api.key")
            return DesktopController(NodeClient(node_url, token, timeout=args.timeout))

        qualification_automation = None
        if args.gate13_ui_playthrough is not None:
            from communityai_desktop.gate13_playthrough import Gate13Playthrough, PlaythroughPlan

            qualification_automation = Gate13Playthrough(
                PlaythroughPlan.load(args.gate13_ui_playthrough),
                args.gate13_ui_evidence,
                screenshot_path=args.gate13_ui_screenshot,
            )
        elif args.resource_ui_playthrough is not None:
            from communityai_desktop.resource_playthrough import ResourcePlaythrough

            qualification_automation = ResourcePlaythrough(args.resource_ui_playthrough, args.resource_ui_evidence)

        if args.probe_only:
            try:
                _write_json(connect().snapshot())
                return 0
            finally:
                if lifecycle is not None:
                    lifecycle.close()

        from communityai_desktop.pyside_shell import run

        updater = None
        if qualification_automation is None and profile is None:
            from communityai_desktop.updater import UpdateManager, installed_root
            from PySide6.QtCore import QStandardPaths

            root = installed_root()
            if root is not None:
                cache = (
                    Path(QStandardPaths.writableLocation(QStandardPaths.GenericCacheLocation)) / "CommunityAI/updates"
                )
                updater = UpdateManager(cache, root=root)

        # Credential and connection errors belong in the window for normal desktop
        # startup. Existing headless installations migrate automatically.
        try:
            return int(
                run(
                    connect=connect,
                    start_minimized=args.started_at_login,
                    activate_existing_instance=not args.started_at_login,
                    before_termination_restore=None if lifecycle is None else lifecycle.close,
                    qualification_automation=qualification_automation,
                    single_instance=qualification_automation is None,
                    updater=updater,
                    **shell_profile_options,
                )
                or 0
            )
        finally:
            if lifecycle is not None:
                lifecycle.close()
    except (CredentialError, NodeClientError, NodeLifecycleError, SingleInstanceError, ValueError, OSError) as exc:
        if anchored and getattr(sys, "frozen", False) and sys.stderr is None:
            # A windowed unprovisioned build must not silently disappear. This
            # does not create the profile, provision a key, or start a service.
            from PySide6.QtWidgets import QApplication, QMessageBox

            application = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, profile.application_name, str(exc))
        parser.exit(2, f"communityai-desktop: {exc}\n")


def volunteer_main(argv: Optional[Sequence[str]] = None) -> int:
    """Dedicated test entry point: arguments cannot select the ordinary profile."""
    return main(argv, forced_profile=VOLUNTEER_PROFILE)


if __name__ == "__main__":
    sys.exit(main())
