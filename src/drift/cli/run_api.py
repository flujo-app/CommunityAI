"""``drift api``: serve an OpenAI-compatible HTTP API backed by a DRIFT-LLM swarm.

Joins the swarm as a client (embeddings and lm_head run locally, blocks run on the swarm) and
exposes /v1/models, /v1/chat/completions and /v1/completions with SSE streaming. Requires the
``api`` extra (fastapi + uvicorn): ``pip install drift[api]``.
"""

import argparse

from hivemind.utils.logging import get_logger, use_hivemind_log_handler

import drift
from drift.model_manifest import ManifestArtifactVerifier, ManifestError, ModelManifest, resolve_manifest_loading
from drift.utils.process_lifetime import tie_child_processes_to_this_process

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(
        prog="drift api",
        description="Serve an OpenAI-compatible HTTP API backed by a DRIFT-LLM swarm",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("model", help="HF repo or local path of the model the swarm is serving")
    parser.add_argument("--initial_peers", nargs="+", required=True, help="Multiaddrs of swarm peers to join via")
    parser.add_argument("--dht_prefix", default=None, help="DHT prefix the swarm's servers announce under")
    parser.add_argument("--revision", default=None, help="Exact model revision to load for legacy/private swarms")
    parser.add_argument("--token", default=None, help="Hugging Face token for gated model artifacts")
    parser.add_argument("--cache_dir", default=None, help="Hugging Face cache directory")
    parser.add_argument(
        "--model_manifest",
        default=None,
        help="Path to a ModelManifest v1. Pins revision, runtime compatibility, and DHT namespace",
    )
    parser.add_argument(
        "--torch_dtype",
        default=None,
        choices=["float32", "float16", "bfloat16"],
        help="dtype for the local embeddings/lm_head (default: manifest dtype, otherwise bfloat16)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind (0.0.0.0 to expose on the network)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--api_key", nargs="*", default=None, help="If set, clients must send Authorization: Bearer <one of these keys>"
    )
    parser.add_argument(
        "--max_concurrent",
        type=int,
        default=1,
        help="Max simultaneous generations (each holds a server-side attention cache)",
    )
    parser.add_argument(
        "--request_timeout",
        type=float,
        default=30,
        help="Max seconds to wait for one swarm RPC before trying a replacement route",
    )
    parser.add_argument(
        "--max_retries",
        type=int,
        default=3,
        help="Max attempts for one swarm step, including the original route",
    )
    parser.add_argument(
        "--default_max_tokens", type=int, default=512, help="max_tokens used when a request does not specify one"
    )
    args = parser.parse_args()
    if args.request_timeout <= 0:
        parser.error("--request_timeout must be positive")
    if args.max_retries < 1:
        parser.error("--max_retries must be at least 1")

    manifest = None
    artifact_verifier = None
    if args.model_manifest is not None:
        try:
            manifest = ModelManifest.load(args.model_manifest)
            manifest.validate_runtime(drift.__version__)
            args.revision, args.dht_prefix = resolve_manifest_loading(
                manifest,
                model_name_or_path=args.model,
                revision=args.revision,
                dht_prefix=args.dht_prefix,
            )
            if args.torch_dtype is None:
                args.torch_dtype = manifest.runtime.dtype
            elif args.torch_dtype != manifest.runtime.dtype:
                raise ManifestError(
                    f"--torch_dtype {args.torch_dtype!r} conflicts with manifest dtype {manifest.runtime.dtype!r}"
                )
            if manifest.runtime.adapter_profile != "none":
                raise ManifestError(
                    "Content-addressed adapter profiles are declared but not executable in this release"
                )
            artifact_verifier = ManifestArtifactVerifier(
                manifest,
                repository=args.model,
                revision=args.revision,
                token=args.token,
                cache_dir=args.cache_dir,
            )
            artifact_verifier.ensure_startup_metadata(include_tokenizer=True)
        except ManifestError as exc:
            parser.error(str(exc))
    elif args.torch_dtype is None:
        args.torch_dtype = "bfloat16"

    try:
        import uvicorn

        from drift.api.server import create_app
    except ImportError as exc:
        raise SystemExit(
            f"drift api requires the 'api' extra (fastapi + uvicorn): pip install drift[api] ({exc})"
        ) from exc

    # Arm this before anything can spawn a p2pd, so a hard-killed API server does not orphan its daemon
    tie_child_processes_to_this_process()

    import torch
    from transformers import AutoTokenizer

    from drift import AutoDistributedModelForCausalLM

    logger.info(f"Loading tokenizer and client-side weights for {args.model}")
    if artifact_verifier is not None:
        tokenizer = AutoTokenizer.from_pretrained(artifact_verifier.snapshot_root, local_files_only=True)
    else:
        tokenizer = AutoTokenizer.from_pretrained(
            args.model,
            revision=args.revision,
            token=args.token,
            cache_dir=args.cache_dir,
        )
    model_kwargs = dict(
        initial_peers=args.initial_peers,
        dht_prefix=args.dht_prefix,
        revision=args.revision,
        manifest_digest=manifest.digest if manifest is not None else None,
        torch_dtype=getattr(torch, args.torch_dtype),
        token=args.token,
        cache_dir=args.cache_dir,
        request_timeout=args.request_timeout,
        max_retries=args.max_retries,
        max_backoff=5,
    )
    if artifact_verifier is not None:
        model_kwargs["artifact_verifier"] = artifact_verifier
    model = AutoDistributedModelForCausalLM.from_pretrained(args.model, **model_kwargs)

    app = create_app(
        model,
        tokenizer,
        model_name=args.model,
        api_keys=args.api_key,
        max_concurrent=args.max_concurrent,
        default_max_tokens=args.default_max_tokens,
    )
    logger.info(f"Serving an OpenAI-compatible API for {args.model} at http://{args.host}:{args.port}/v1")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
