"""Inspect and validate a content-addressed ModelManifest v1."""

import argparse
import json
import sys
from pathlib import Path

from drift.model_manifest import ManifestError, ModelManifest, _select_snapshot_artifacts, create_manifest_from_snapshot


def _verify(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="drift manifest",
        description="Validate and inspect a DRIFT ModelManifest v1 without joining a swarm",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("manifest", help="Path to a ModelManifest v1 JSON file")
    parser.add_argument("--artifact_root", help="If set, verify every declared artifact relative to this directory")
    parser.add_argument("--canonical", action="store_true", help="Print canonical manifest JSON after validation")
    args = parser.parse_args(argv)

    try:
        manifest = ModelManifest.load(args.manifest)
        if args.artifact_root:
            manifest.verify_artifacts(args.artifact_root)
    except ManifestError as exc:
        parser.error(str(exc))

    if args.canonical:
        print(manifest.canonical_json())
    else:
        print(
            json.dumps(
                {
                    "name": manifest.name,
                    "repository": manifest.source.repository,
                    "revision": manifest.source.revision,
                    "digest": manifest.digest_id,
                    "dht_prefix": manifest.dht_prefix,
                    "artifacts_verified": bool(args.artifact_root),
                },
                indent=2,
            )
        )


def _card_license(model_info):
    card_data = getattr(model_info, "card_data", None)
    if card_data is None:
        return None
    if isinstance(card_data, dict):
        return card_data.get("license")
    return getattr(card_data, "license", None)


def _generate(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="drift manifest generate",
        description="Generate a deterministic ModelManifest v1 from an immutable Hugging Face snapshot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("repository", help="Hugging Face model repository, e.g. org/model")
    parser.add_argument(
        "--revision", default="main", help="Revision to resolve; output always pins its full commit SHA"
    )
    parser.add_argument(
        "--artifact_root",
        help="Use an already complete local snapshot (requires a full --revision and explicit --license)",
    )
    parser.add_argument("--name", help="Human-readable model name (default: repository basename)")
    parser.add_argument("--alias", action="append", default=[], help="OpenAI API alias; may be repeated")
    parser.add_argument("--license", dest="license_name", help="SPDX license identifier or exact upstream label")
    parser.add_argument(
        "--gated",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Whether access to the source artifacts is gated",
    )
    parser.add_argument("--minimum_version", default="2.3.0.dev0")
    parser.add_argument("--maximum_version_exclusive", default="2.4.0")
    parser.add_argument("--attention_implementation", choices=("auto", "eager", "sdpa"), default="auto")
    parser.add_argument("--dtype", choices=("float32", "float16", "bfloat16"), default="bfloat16")
    parser.add_argument("--quantization", choices=("none", "int8", "nf4", "fp8_dequant"), default="none")
    parser.add_argument("--token", help="Hugging Face token for gated repositories")
    parser.add_argument("--cache_dir", help="Hugging Face cache directory")
    parser.add_argument("--output", help="Write pretty, deterministic JSON to this path instead of stdout")
    args = parser.parse_args(argv)

    try:
        if args.artifact_root:
            revision = args.revision
            artifact_root = Path(args.artifact_root)
            if args.license_name is None:
                raise ManifestError("--license is required with --artifact_root")
            if args.gated is None:
                raise ManifestError("--gated or --no-gated is required with --artifact_root")
            license_name, gated = args.license_name, args.gated
        else:
            from huggingface_hub import HfApi, snapshot_download

            api = HfApi()
            info = api.model_info(
                args.repository,
                revision=args.revision,
                files_metadata=True,
                token=args.token,
            )
            revision = str(info.sha).lower()
            repo_files = sorted(sibling.rfilename for sibling in info.siblings)

            metadata_patterns = [
                path
                for path in repo_files
                if path == "config.json"
                or path.endswith(".index.json")
                or "tokenizer" in Path(path).name
                or Path(path).name
                in {
                    "added_tokens.json",
                    "merges.txt",
                    "sentencepiece.bpe.model",
                    "special_tokens_map.json",
                    "spiece.model",
                    "vocab.json",
                    "vocab.txt",
                }
                or path == "chat_template.jinja"
                or path.startswith("chat_templates/")
            ]
            artifact_root = Path(
                snapshot_download(
                    args.repository,
                    revision=revision,
                    cache_dir=args.cache_dir,
                    token=args.token,
                    allow_patterns=metadata_patterns,
                )
            )
            selected = _select_snapshot_artifacts(repo_files, artifact_root)
            artifact_root = Path(
                snapshot_download(
                    args.repository,
                    revision=revision,
                    cache_dir=args.cache_dir,
                    token=args.token,
                    allow_patterns=sorted(selected),
                )
            )
            license_name = args.license_name or _card_license(info)
            if not license_name:
                raise ManifestError("The Hub model card has no license; pass --license explicitly")
            gated = args.gated if args.gated is not None else bool(info.gated)

        manifest = create_manifest_from_snapshot(
            repository=args.repository,
            revision=revision,
            artifact_root=artifact_root,
            name=args.name or args.repository.rsplit("/", 1)[-1],
            aliases=args.alias,
            license_name=license_name,
            gated=gated,
            minimum_version=args.minimum_version,
            maximum_version_exclusive=args.maximum_version_exclusive,
            attention_implementation=args.attention_implementation,
            dtype=args.dtype,
            quantization=args.quantization,
        )
    except ManifestError as exc:
        parser.error(str(exc))
    except Exception as exc:
        parser.error(f"Could not generate manifest: {exc}")

    output = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8", newline="\n")
        print(f"Wrote {output_path} ({manifest.digest_id})")
    else:
        print(output, end="")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        _generate(sys.argv[2:])
    else:
        _verify(sys.argv[1:])
