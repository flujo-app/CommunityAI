"""Command-line launcher shared by the desktop shell prototypes."""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from communityai_desktop_spike.acceptance import fake_node, run_self_test
from communityai_desktop_spike.client import NodeClient, NodeClientError, normalize_loopback_url
from communityai_desktop_spike.controller import DesktopController
from communityai_desktop_spike.credentials import (
    DEFAULT_CREDENTIAL_ACCOUNT,
    DEFAULT_CREDENTIAL_SERVICE,
    CredentialError,
    NativeCredentialStore,
    load_private_key_file,
)


def build_parser(*, default_shell: Optional[str] = None, locked_shell: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CommunityAI desktop shell experiment")
    if locked_shell:
        if default_shell not in ("pyside", "webview"):
            raise ValueError("a packaged launcher must select one desktop shell")
        parser.set_defaults(shell=default_shell)
    else:
        parser.add_argument("--shell", choices=("pyside", "webview"), default=default_shell or "pyside")
    parser.add_argument("--node-url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--control-key-file", type=Path)
    parser.add_argument("--credential-service", default=DEFAULT_CREDENTIAL_SERVICE)
    parser.add_argument("--credential-account", default=DEFAULT_CREDENTIAL_ACCOUNT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--store-control-key", action="store_true")
    action.add_argument("--delete-control-key", action="store_true")
    action.add_argument("--check-runtime", action="store_true")
    action.add_argument("--self-test", action="store_true")
    action.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--probe-only", action="store_true")
    return parser


def _shell_module(shell: str):
    return importlib.import_module(f"communityai_desktop_spike.{shell}_shell")


def _credential_store(args: argparse.Namespace) -> NativeCredentialStore:
    return NativeCredentialStore(args.credential_service, args.credential_account)


def _load_control_token(args: argparse.Namespace) -> str:
    if args.control_key_file is not None:
        return load_private_key_file(args.control_key_file)
    return _credential_store(args).get()


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    default_shell: Optional[str] = None,
    locked_shell: bool = False,
) -> int:
    parser = build_parser(default_shell=default_shell, locked_shell=locked_shell)
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            print(json.dumps(run_self_test(), sort_keys=True))
            return 0
        if args.check_runtime:
            print(json.dumps(_shell_module(args.shell).check_runtime(), sort_keys=True))
            return 0
        if args.ui_self_test:
            with fake_node() as (url, token):
                controller = DesktopController(NodeClient(url, token))
                return int(_shell_module(args.shell).run(controller, auto_close_seconds=1.0) or 0)
        if args.store_control_key:
            secret = getpass.getpass("Local node control credential: ")
            _credential_store(args).set(secret)
            print(f"Stored control credential for account {args.credential_account!r}")
            return 0
        if args.delete_control_key:
            deleted = _credential_store(args).delete()
            print("Deleted control credential" if deleted else "No stored control credential")
            return 0

        # Validate the destination before opening either credential source.
        node_url = normalize_loopback_url(args.node_url)
        client = NodeClient(node_url, _load_control_token(args), timeout=args.timeout)
        controller = DesktopController(client)
        if args.probe_only:
            print(json.dumps(controller.snapshot(), sort_keys=True))
            return 0
        return int(_shell_module(args.shell).run(controller) or 0)
    except (CredentialError, NodeClientError, ValueError) as exc:
        parser.exit(2, f"communityai-desktop-spike: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
