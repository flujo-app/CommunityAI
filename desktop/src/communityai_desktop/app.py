"""Command-line and packaged entry point for the CommunityAI desktop."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from communityai_desktop import __version__
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
from communityai_desktop.startup import LOGIN_STARTUP_FLAG, SingleInstanceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CommunityAI desktop")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--store-control-key", action="store_true")
    action.add_argument("--delete-control-key", action="store_true")
    action.add_argument("--check-runtime", action="store_true")
    action.add_argument("--self-test", action="store_true")
    action.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--onboarding-ui-self-test", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--capture-ui", type=Path, help=argparse.SUPPRESS)
    action.add_argument("--probe-only", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--gate13-ui-playthrough", type=Path, help=argparse.SUPPRESS)
    return parser


def _credential_store(args: argparse.Namespace) -> NativeCredentialStore:
    return NativeCredentialStore(args.credential_service, args.credential_account)


def _write_json(value: Any) -> None:
    """Write diagnostics when attached to a console; windowed bundles have none."""
    if sys.stdout is not None:
        print(json.dumps(value, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.gate13_ui_playthrough is None:
        if args.gate13_ui_evidence is not None or args.gate13_ui_screenshot is not None:
            parser.error("Gate 13 evidence options require --gate13-ui-playthrough")
    elif args.gate13_ui_evidence is None:
        parser.error("--gate13-ui-playthrough requires --gate13-ui-evidence")
    try:
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
            else NodeLifecycleSupervisor(
                node_url,
                credential_store,
                config_path=args.node_config,
                data_dir=args.node_data_dir,
                bootstrap_config_path=args.bootstrap_config,
                client_timeout=args.timeout,
            )
        )

        def connect() -> DesktopController:
            if lifecycle is not None:
                return DesktopController(lifecycle.ensure_client())
            token = credential_store.get_or_migrate()
            return DesktopController(NodeClient(node_url, token, timeout=args.timeout))

        qualification_automation = None
        if args.gate13_ui_playthrough is not None:
            from communityai_desktop.gate13_playthrough import Gate13Playthrough, PlaythroughPlan

            qualification_automation = Gate13Playthrough(
                PlaythroughPlan.load(args.gate13_ui_playthrough),
                args.gate13_ui_evidence,
                screenshot_path=args.gate13_ui_screenshot,
            )

        if args.probe_only:
            try:
                _write_json(connect().snapshot())
                return 0
            finally:
                if lifecycle is not None:
                    lifecycle.close()

        from communityai_desktop.pyside_shell import run

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
                )
                or 0
            )
        finally:
            if lifecycle is not None:
                lifecycle.close()
    except (CredentialError, NodeClientError, NodeLifecycleError, SingleInstanceError, ValueError) as exc:
        parser.exit(2, f"communityai-desktop: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
