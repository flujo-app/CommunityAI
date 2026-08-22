"""Manage persistent libp2p identities and signed rotation/revocation records."""

import argparse
import json
import os
import sys
from pathlib import Path

from drift.protocol_identity import (
    NodeIdentity,
    ProtocolSecurityError,
    RevocationStore,
    SignedRecord,
    create_revocation_record,
    create_rotation_record,
    verify_rotation_record,
)


def _identity_summary(identity: NodeIdentity) -> dict:
    return {
        "key_id": identity.key_id,
        "peer_id": identity.peer_id.to_base58(),
        "public_key": identity.public_key_b64,
    }


def _write_json(path: str, value: dict, *, force: bool) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path == "-":
        sys.stdout.write(rendered)
        return
    mode = "w" if force else "x"
    try:
        with Path(path).open(mode, encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except FileExistsError as exc:
        raise ProtocolSecurityError(f"Refusing to overwrite {path}; pass --force if intentional") from exc


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _require_distinct_paths(*named_paths: tuple[str, str]) -> None:
    seen = {}
    for name, path in named_paths:
        if path == "-":
            continue
        normalized = _normalized_path(path)
        if normalized in seen:
            raise ProtocolSecurityError(f"{name} must not overwrite {seen[normalized]}")
        seen[normalized] = name


def _require_output_available(path: str, *, force: bool) -> None:
    if path != "-" and Path(path).exists() and not force:
        raise ProtocolSecurityError(f"Refusing to overwrite {path}; pass --force if intentional")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift identity",
        description="Create and rotate public-swarm identities and validate trust records",
    )
    commands = parser.add_subparsers(dest="identity_command", required=True)

    create = commands.add_parser("create", help="Create a new persistent libp2p identity")
    create.add_argument("path")
    create.add_argument("--force", action="store_true", help="Replace an existing identity")

    inspect = commands.add_parser("inspect", help="Print the public identity and PeerID")
    inspect.add_argument("path")

    rotate = commands.add_parser("rotate", help="Create a new identity and a dual-signed rotation proof")
    rotate.add_argument("old_identity")
    rotate.add_argument("new_identity")
    rotate.add_argument("--output", required=True, help="Rotation record path, or - for stdout")
    rotate.add_argument("--sequence", type=int, default=0)
    rotate.add_argument("--force", action="store_true", help="Replace output and new identity if they exist")

    revoke = commands.add_parser("revoke", help="Create a self-signed permanent revocation")
    revoke.add_argument("identity")
    revoke.add_argument("--output", required=True, help="Revocation record path, or - for stdout")
    revoke.add_argument("--reason", default="")
    revoke.add_argument("--sequence", type=int, default=0)
    revoke.add_argument("--force", action="store_true")

    verify = commands.add_parser("verify", help="Verify one or more rotation/revocation records as a bundle")
    verify.add_argument("records", nargs="+")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.identity_command == "create":
            identity = NodeIdentity.create(args.path, overwrite=args.force)
            print(json.dumps(_identity_summary(identity), indent=2, sort_keys=True))
        elif args.identity_command == "inspect":
            print(json.dumps(_identity_summary(NodeIdentity.load(args.path)), indent=2, sort_keys=True))
        elif args.identity_command == "rotate":
            _require_distinct_paths(
                ("old identity", args.old_identity),
                ("new identity", args.new_identity),
                ("rotation output", args.output),
            )
            _require_output_available(args.output, force=args.force)
            old_identity = NodeIdentity.load(args.old_identity)
            new_identity = NodeIdentity.create(args.new_identity, overwrite=args.force)
            record = create_rotation_record(old_identity, new_identity, sequence=args.sequence)
            _write_json(args.output, record, force=args.force)
            print(f"rotation {old_identity.key_id} -> {new_identity.key_id}")
        elif args.identity_command == "revoke":
            _require_distinct_paths(("identity", args.identity), ("revocation output", args.output))
            _require_output_available(args.output, force=args.force)
            identity = NodeIdentity.load(args.identity)
            record = create_revocation_record(identity, reason=args.reason, sequence=args.sequence)
            _write_json(args.output, record, force=args.force)
            print(f"revoked {identity.key_id}")
        else:
            store = RevocationStore.from_files(args.records)
            for path in args.records:
                value = json.loads(Path(path).read_text(encoding="utf-8"))
                values = value if isinstance(value, list) else [value]
                for record in values:
                    if record.get("kind") == "identity_rotation":
                        verify_rotation_record(record)
                    else:
                        signed = SignedRecord.from_dict(record)
                        signed.verify(expected_kind="identity_revocation")
            print(
                f"valid trust bundle: {len(store.successors)} rotation(s), "
                f"{len(store.revoked_key_ids)} revocation(s)"
            )
    except ProtocolSecurityError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
