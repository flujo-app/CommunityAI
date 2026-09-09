"""Run an optional peer that owns input/output processing for text-only consumers."""

import argparse
import signal
import time
from pathlib import Path

from hivemind import DHT

from drift.model_manifest import ModelManifest
from drift.protocol_identity import NodeIdentity
from drift.server.text_peer import TextPeerService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--initial_peers", nargs="+", required=True)
    parser.add_argument("--identity_path", type=Path, required=True)
    parser.add_argument("--cache_dir", type=Path, required=True)
    parser.add_argument("--host_maddrs", nargs="+", default=["/ip4/0.0.0.0/tcp/31337"])
    parser.add_argument("--announce_maddrs", nargs="+")
    parser.add_argument("--max_context_tokens", type=int, default=2048)
    parser.add_argument("--max_output_tokens", type=int, default=512)
    args = parser.parse_args()
    manifest = ModelManifest.load(args.manifest)
    identity = NodeIdentity.ensure(args.identity_path)
    dht = DHT(
        initial_peers=args.initial_peers,
        identity_path=str(args.identity_path),
        host_maddrs=args.host_maddrs,
        announce_maddrs=args.announce_maddrs,
        tls=True,
        start=True,
    )
    service = TextPeerService(
        dht,
        identity,
        manifest,
        initial_peers=args.initial_peers,
        cache_dir=str(args.cache_dir),
        max_context_tokens=args.max_context_tokens,
        max_output_tokens=args.max_output_tokens,
    ).start()
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            if service.error:
                raise RuntimeError("Text peer startup failed; check the log")
            time.sleep(1)
    finally:
        service.close()
        dht.shutdown()


if __name__ == "__main__":
    main()
