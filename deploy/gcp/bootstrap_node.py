"""Minimal CommunityAI DHT bootstrap process for small CPU-only hosts."""

from __future__ import annotations

import argparse
import asyncio
import signal
import threading
from concurrent.futures import Future
from contextlib import asynccontextmanager
from functools import partial
from secrets import token_hex
from typing import Optional

from hivemind.dht import DHT, DHTNode
from hivemind.p2p import P2P, P2PContext, PeerID, ServicerBase
from hivemind.proto import dht_pb2
from hivemind.utils import get_logger
from hivemind.utils.logging import use_hivemind_log_handler
from hivemind.utils.networking import log_visible_maddrs

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__name__)


class ReachabilityProtocol(ServicerBase):
    """Let contributors ask this public peer to verify their reachability."""

    def __init__(self, *, probe: Optional[P2P] = None, wait_timeout: float = 5.0):
        self.probe = probe
        self.wait_timeout = wait_timeout
        self._event_loop = None
        self._stop = None

    async def call_check(self, remote_peer: PeerID, *, check_peer: PeerID) -> Optional[bool]:
        try:
            request = dht_pb2.PingRequest(peer=dht_pb2.NodeInfo(node_id=check_peer.to_bytes()))
            timeout = self.wait_timeout if check_peer == remote_peer else self.wait_timeout * 2
            response = await self.get_stub(self.probe, remote_peer).rpc_check(request, timeout=timeout)
            return response.available
        except Exception:
            logger.debug("Could not check peer reachability", exc_info=True)
            return None

    async def rpc_check(self, request: dht_pb2.PingRequest, context: P2PContext) -> dht_pb2.PingResponse:
        check_peer = PeerID(request.peer.node_id)
        response = dht_pb2.PingResponse(available=True)
        if check_peer != context.local_id:
            response.available = await self.call_check(check_peer, check_peer=check_peer) is True
        return response

    @asynccontextmanager
    async def serve(self, p2p: P2P):
        try:
            await self.add_p2p_handlers(p2p)
            yield self
        finally:
            await self.remove_p2p_handlers(p2p)

    @classmethod
    def attach_to_dht(cls, dht: DHT, *, await_ready: bool = False) -> "ReachabilityProtocol":
        protocol = cls()
        ready: Future[bool] = Future()

        async def serve() -> None:
            try:
                common_p2p = await dht.replicate_p2p()
                protocol._event_loop = asyncio.get_event_loop()
                protocol._stop = asyncio.Event()
                initial_peers = [str(address) for address in await common_p2p.get_visible_maddrs(latest=True)]
                for peer in await common_p2p.list_peers():
                    initial_peers.extend(f"{address}/p2p/{peer.peer_id}" for address in peer.addrs)
                protocol.probe = await P2P.create(
                    initial_peers,
                    dht_mode="client",
                    use_relay=False,
                    auto_nat=False,
                    nat_port_map=False,
                    no_listen=True,
                    startup_timeout=60,
                )
                ready.set_result(True)
                async with protocol.serve(common_p2p):
                    await protocol._stop.wait()
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(exc)
                logger.exception("Reachability service stopped unexpectedly")
            finally:
                if protocol.probe is not None:
                    await protocol.probe.shutdown()

        threading.Thread(target=partial(asyncio.run, serve()), name="reachability", daemon=True).start()
        if await_ready:
            ready.result(timeout=90)
        return protocol

    def shutdown(self) -> None:
        if self._event_loop is not None and self._stop is not None:
            self._event_loop.call_soon_threadsafe(self._stop.set)


async def report_status(_dht: DHT, node: DHTNode) -> None:
    logger.info(
        "%d DHT nodes in the local routing table; %d locally stored keys",
        len(node.protocol.routing_table.uid_to_peer_id) + 1,
        len(node.protocol.storage),
    )
    await node.get(f"heartbeat_{token_hex(16)}", latest=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity-path", required=True)
    parser.add_argument("--host-maddr", default="/ip4/0.0.0.0/tcp/31337")
    parser.add_argument("--announce-maddr", required=True)
    parser.add_argument("--refresh-period", type=int, default=30)
    args = parser.parse_args()

    stopping = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stopping.set())

    dht = DHT(
        start=True,
        host_maddrs=[args.host_maddr],
        announce_maddrs=[args.announce_maddr],
        identity_path=args.identity_path,
        use_relay=True,
        use_auto_relay=False,
    )
    log_visible_maddrs(dht.get_visible_maddrs(), only_p2p=False)
    reachability = ReachabilityProtocol.attach_to_dht(dht, await_ready=True)
    try:
        while not stopping.wait(args.refresh_period):
            dht.run_coroutine(report_status, return_future=False)
    finally:
        reachability.shutdown()
        dht.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
