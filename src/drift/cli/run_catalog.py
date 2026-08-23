"""Create and verify signed model catalogs and their offline trust roots."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Mapping

from drift.model_catalog import (
    CATALOG_ROOT_SCHEMA_VERSION,
    CATALOG_SCHEMA_VERSION,
    CatalogRollbackGuard,
    CatalogSigningKey,
    CatalogTrustedKey,
    CatalogTrustRoot,
    ModelCatalog,
    ModelCatalogError,
    SignedModelCatalog,
    _load_strict_json,
)


def _write_json(path: str, value: Mapping[str, Any], *, force: bool) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path == "-":
        sys.stdout.write(rendered)
        return
    resolved = Path(path).expanduser().resolve()
    if force:
        temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
            os.replace(temporary, resolved)
        except OSError as exc:
            raise ModelCatalogError(f"Could not write {resolved}: {exc}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return
    try:
        with resolved.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except FileExistsError as exc:
        raise ModelCatalogError(f"Refusing to overwrite {path}; pass --force if intentional") from exc
    except OSError as exc:
        raise ModelCatalogError(f"Could not write {resolved}: {exc}") from exc


def _write_status(message: str, *, json_output: str) -> None:
    print(message, file=sys.stderr if json_output == "-" else sys.stdout)


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _require_distinct_paths(*named_paths: tuple[str, str]) -> None:
    seen = {}
    for name, path in named_paths:
        if path == "-":
            continue
        normalized = _normalized_path(path)
        if normalized in seen:
            raise ModelCatalogError(f"{name} must not overwrite {seen[normalized]}")
        seen[normalized] = name


def _require_output_available(path: str, *, force: bool) -> None:
    if path != "-" and Path(path).expanduser().exists() and not force:
        raise ModelCatalogError(f"Refusing to overwrite {path}; pass --force if intentional")


def _read_json(path: str, *, name: str) -> Mapping[str, Any]:
    try:
        source = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ModelCatalogError(f"Could not read {name} {path}: {exc}") from exc
    return _load_strict_json(source, name=name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift catalog", description="Create offline signing keys and verify threshold-signed model catalogs"
    )
    commands = parser.add_subparsers(dest="catalog_command", required=True)

    keygen = commands.add_parser("keygen", help="Create an independent Ed25519 catalog signing key")
    keygen.add_argument("private_key")
    keygen.add_argument("--public-output", required=True, help="Trusted public-key JSON path, or - for stdout")
    keygen.add_argument("--force", action="store_true")

    root = commands.add_parser("root", help="Build a local trust root from public catalog keys")
    root.add_argument("--catalog-id", required=True)
    root.add_argument("--threshold", required=True, type=int)
    root.add_argument("--key", action="append", required=True, help="Public-key JSON; repeat for every signer")
    root.add_argument("--output", required=True)
    root.add_argument("--force", action="store_true")

    sign = commands.add_parser("sign", help="Sign a catalog payload or add a signature to an envelope")
    sign.add_argument("catalog")
    sign.add_argument("--key", required=True, help="Offline Ed25519 private key")
    sign.add_argument("--output", required=True)
    sign.add_argument("--force", action="store_true")

    verify = commands.add_parser("verify", help="Verify signatures, expiry, and optional rollback state")
    verify.add_argument("catalog")
    verify.add_argument("--root", required=True)
    verify.add_argument(
        "--state", help="Persist the last accepted sequence and reject rollback/equivocation on later runs"
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.catalog_command == "keygen":
            _require_distinct_paths(("private key", args.private_key), ("public output", args.public_output))
            _require_output_available(args.private_key, force=args.force)
            _require_output_available(args.public_output, force=args.force)
            key = CatalogSigningKey.generate()
            key.save(args.private_key, overwrite=args.force)
            _write_json(args.public_output, key.trusted_key.to_dict(), force=args.force)
            _write_status(f"created catalog key {key.key_id}", json_output=args.public_output)
        elif args.catalog_command == "root":
            _require_distinct_paths(
                ("trust-root output", args.output),
                *((f"public key {index}", path) for index, path in enumerate(args.key)),
            )
            keys = tuple(
                CatalogTrustedKey.from_dict(_read_json(path, name="catalog public key"), name=f"key {index}")
                for index, path in enumerate(args.key)
            )
            root = CatalogTrustRoot.from_dict(
                {
                    "schema_version": CATALOG_ROOT_SCHEMA_VERSION,
                    "catalog_id": args.catalog_id,
                    "threshold": args.threshold,
                    "keys": [key.to_dict() for key in keys],
                }
            )
            _write_json(args.output, root.to_dict(), force=args.force)
            _write_status(
                f"created {root.threshold}-of-{len(root.keys)} trust root for {root.catalog_id}",
                json_output=args.output,
            )
        elif args.catalog_command == "sign":
            _require_distinct_paths(
                ("catalog input", args.catalog), ("signing key", args.key), ("catalog output", args.output)
            )
            source = _read_json(args.catalog, name="model catalog")
            if set(source) == {"schema_version", "signed", "signatures"}:
                envelope = SignedModelCatalog.from_dict(source)
            else:
                envelope = SignedModelCatalog(
                    schema_version=CATALOG_SCHEMA_VERSION,
                    signed=ModelCatalog.from_dict(source),
                    signatures=(),
                )
            envelope = envelope.add_signature(CatalogSigningKey.load(args.key))
            _write_json(args.output, envelope.to_dict(), force=args.force)
            _write_status(f"signed catalog sequence {envelope.signed.sequence}", json_output=args.output)
        else:
            if args.state:
                _require_distinct_paths(
                    ("catalog input", args.catalog), ("trust root", args.root), ("rollback state", args.state)
                )
            envelope = SignedModelCatalog.load(args.catalog)
            root = CatalogTrustRoot.load(args.root)
            rollback_guard = CatalogRollbackGuard.load(args.state) if args.state else None
            catalog = envelope.verify(root, rollback_guard=rollback_guard)
            if args.state:
                rollback_guard.save(args.state)
            print(
                f"valid catalog {catalog.catalog_id} sequence {catalog.sequence}: "
                f"{len(catalog.rungs)} rung(s), {len(catalog.models)} model(s), digest {catalog.digest}"
            )
    except ModelCatalogError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
