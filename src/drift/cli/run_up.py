"""``drift up`` -- the one-command way to run a DRIFT-LLM private swarm.

The first machine::

    drift up meta-llama/Llama-3.1-8B-Instruct

starts a *new private swarm*, serves as many blocks as fit, and prints a copy-paste
join command. Every other machine runs that command to add its compute::

    drift up meta-llama/Llama-3.1-8B-Instruct --join drift://<peer_id>@<host>:<port>

It is a thin front-end over ``drift server`` (drift.cli.run_server): every server
flag still works, and ``--join`` simply fills in ``--initial_peers`` for you while
defaulting to a fresh private swarm.
"""

from pathlib import Path

from hivemind.utils.logging import get_logger

from drift.cli.run_server import build_parser, serve, server_from_args
from drift.model_manifest import ManifestError
from drift.utils.join_token import encode_join_token, parse_join, select_advertisable_maddrs

logger = get_logger(__name__)

# A stable identity for the first node keeps its join address constant across restarts,
# so a token you shared once keeps working. Joining nodes use a fresh identity each run.
DEFAULT_IDENTITY_PATH = Path.home() / ".cache" / "drift" / "identity.key"


def build_up_parser():
    parser = build_parser()
    parser.add_argument(
        "--join",
        type=str,
        default=None,
        help="Add this machine to an existing swarm. Accepts a drift:// join token (printed by "
        "the first node) or a raw multiaddr. Omit to start a new private swarm.",
    )
    return parser


def main():
    parser = build_up_parser()
    args = vars(parser.parse_args())
    args.pop("config", None)

    join = args.pop("join")
    model_name = args.get("model") or args.get("converted_model_name_or_path")

    if join:
        # Explicit join target wins and puts us in the existing swarm.
        args["initial_peers"] = parse_join(join)
        args["new_swarm"] = False
    elif args.get("new_swarm") or args.get("initial_peers"):
        # A power user set --new_swarm or --initial_peers directly; respect their choice.
        pass
    else:
        # The friendly default: start a fresh private swarm.
        args["new_swarm"] = True

    if args.get("new_swarm") and not args.get("identity_path"):
        DEFAULT_IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        args["identity_path"] = str(DEFAULT_IDENTITY_PATH)

    is_first_node = bool(args.get("new_swarm"))
    try:
        server = server_from_args(args)
    except ManifestError as exc:
        parser.error(str(exc))

    def _on_ready(srv):
        if is_first_node:
            _print_join_banner(srv, model_name)

    serve(server, model=model_name, on_ready=_on_ready)


def _print_join_banner(server, model_name: str) -> None:
    """Print copy-paste instructions for adding more machines to this new swarm."""
    maddrs = [str(a) for a in server.dht.get_visible_maddrs()]
    # Prefer routable interfaces; on a lone dev box only loopback exists, so fall back to it.
    advertisable = select_advertisable_maddrs(maddrs) or select_advertisable_maddrs(maddrs, include_loopback=True)

    tokens = []
    for maddr in advertisable:
        try:
            tokens.append(encode_join_token(maddr))
        except ValueError:
            continue  # skip anything that isn't a plain /tcp/.../p2p address

    model_arg = model_name or "<model>"
    width = 74
    lines = ["", "=" * width, "  DRIFT-LLM private swarm is live.".ljust(width)]
    if tokens:
        lines += [
            "  Add more machines with:".ljust(width),
            "",
            f"      drift up {model_arg} \\",
            f"          --join {tokens[0]}",
        ]
        if len(tokens) > 1:
            lines += ["", "  Other reachable addresses (if the first isn't routable for a peer):"]
            lines += [f"      {token}" for token in tokens[1:]]
    else:
        # Extremely unlikely: no plain-tcp address at all. Show raw multiaddrs so the user
        # can still join manually with `--join /ip4/...`.
        lines += ["  Add more machines with `--join` and one of these multiaddrs:"]
        lines += [f"      {maddr}" for maddr in (maddrs or ["<none visible>"])]
    lines += ["=" * width, ""]
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
