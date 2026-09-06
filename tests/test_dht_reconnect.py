import asyncio
from types import SimpleNamespace

import pytest
from hivemind.dht.node import Blacklist
from hivemind.p2p import PeerID

from drift.utils.dht import _reconnect_dht_if_isolated

SEED = "/dns4/bootstrap.communityai.flujo.com.co/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo"


@pytest.mark.parametrize("state", ["healthy", "isolated", "backed-off", "unreachable"])
def test_reconnect_keeps_live_protocol_and_requires_validated_seed_response(state):
    peers = {"existing": "peer"} if state == "healthy" else {}
    calls = []

    async def connect(peer, endpoints):
        assert {str(endpoint) for endpoint in endpoints} == {SEED.rsplit("/p2p/", 1)[0]}

    async def ping(peer, *, validate, strict):
        assert validate and strict
        calls.append(peer)
        await asyncio.sleep(0)
        if state == "unreachable":
            raise RuntimeError("seed did not validate")
        # Match Hivemind's scheduled routing-table update.
        asyncio.get_running_loop().call_soon(peers.update, {"seed": peer})
        return "seed"

    protocol = SimpleNamespace(routing_table=SimpleNamespace(uid_to_peer_id=peers), call_ping=ping)
    node = SimpleNamespace(
        protocol=protocol, p2p=SimpleNamespace(_client=SimpleNamespace(connect=connect)), blacklist=Blacklist(5, 2)
    )
    dht = SimpleNamespace(initial_peers=[SEED, SEED])
    seed_peer = PeerID.from_base58(SEED.rsplit("/p2p/", 1)[1])
    if state == "backed-off":
        peers["seed"] = seed_peer
        node.blacklist.register_failure(seed_peer)

    async def run():
        await asyncio.gather(*(_reconnect_dht_if_isolated(dht, node) for _ in range(2)))

    asyncio.run(run())
    assert node.protocol is protocol
    assert len(calls) == {"healthy": 0, "isolated": 1, "backed-off": 1, "unreachable": 2}[state]
    assert bool(peers) is (state != "unreachable")
    if state == "backed-off":
        assert seed_peer not in node.blacklist
