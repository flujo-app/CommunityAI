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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CommunityAI desktop")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--node-url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--credential-service", default=DEFAULT_CREDENTIAL_SERVICE, help=argparse.SUPPRESS)
    parser.add_argument("--credential-account", default=DEFAULT_CREDENTIAL_ACCOUNT, help=argparse.SUPPRESS)
    parser.add_argument("--capture-page", type=int, default=0, help=argparse.SUPPRESS)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--store-control-key", action="store_true")
    action.add_argument("--delete-control-key", action="store_true")
    action.add_argument("--check-runtime", action="store_true")
    action.add_argument("--self-test", action="store_true")
    action.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--onboarding-ui-self-test", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--capture-ui", type=Path, help=argparse.SUPPRESS)
    action.add_argument("--probe-only", action="store_true", help=argparse.SUPPRESS)
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
                return int(run(DesktopController(NodeClient(url, token)), auto_close_seconds=1.0) or 0)
        if args.onboarding_ui_self_test:
            from communityai_desktop.pyside_shell import run

            def missing_credential():
                raise CredentialError("no local-node control credential is stored")

            return int(
                run(
                    connect=missing_credential,
                    auto_close_seconds=1.0,
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

        def connect() -> DesktopController:
            token = credential_store.get_or_migrate()
            return DesktopController(NodeClient(node_url, token, timeout=args.timeout))

        if args.probe_only:
            _write_json(connect().snapshot())
            return 0

        from communityai_desktop.pyside_shell import run

        # Credential and connection errors belong in the window for normal desktop
        # startup. Existing headless installations migrate automatically.
        return int(run(connect=connect) or 0)
    except (CredentialError, NodeClientError, ValueError) as exc:
        parser.exit(2, f"communityai-desktop: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
