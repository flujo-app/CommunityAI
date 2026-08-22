"""``drift api``: serve an OpenAI-compatible HTTP API backed by a DRIFT-LLM swarm.

Joins the swarm as a client (embeddings and lm_head run locally, blocks run on the swarm) and
exposes /v1/models, /v1/chat/completions and /v1/completions with SSE streaming. Requires the
``api`` extra (fastapi + uvicorn): ``pip install drift[api]``.
"""

import argparse

from hivemind.utils.logging import get_logger, use_hivemind_log_handler

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
    parser.add_argument(
        "--torch_dtype",
        default="bfloat16",
        choices=["float32", "float16", "bfloat16"],
        help="dtype for the local embeddings/lm_head (match the servers' dtype)",
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
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoDistributedModelForCausalLM.from_pretrained(
        args.model,
        initial_peers=args.initial_peers,
        dht_prefix=args.dht_prefix,
        torch_dtype=getattr(torch, args.torch_dtype),
        request_timeout=args.request_timeout,
        max_retries=args.max_retries,
        max_backoff=5,
    )

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
