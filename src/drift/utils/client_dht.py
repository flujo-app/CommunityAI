"""Bounded client discovery startup when saved peers have gone offline."""

from hivemind.utils import get_logger

logger = get_logger(__name__)


def create_client_dht(*, initial_peers=(), start=True, startup_timeout=15.0, **kwargs):
    from hivemind import DHT

    peers = tuple(dict.fromkeys(initial_peers))
    if not start:
        return DHT(initial_peers=list(peers), start=False, startup_timeout=startup_timeout, **kwargs)
    # Joining a set containing dead cached addresses can time out even when
    # the configured bootstrap is healthy. One successful seed is sufficient
    # to discover the rest of the mesh. Keep later seeds as bounded fallbacks.
    candidates = [(peer,) for peer in peers] or [()]
    last_error = None
    for candidate in candidates:
        dht = None
        try:
            dht = DHT(initial_peers=list(candidate), start=False, startup_timeout=startup_timeout, **kwargs)
            dht.run_in_background(timeout=startup_timeout)
            return dht
        except BaseException as exc:
            if dht is not None:
                try:
                    dht.shutdown()
                except Exception:
                    logger.exception("Failed to close unsuccessful discovery startup")
            if not isinstance(exc, Exception):
                raise
            last_error = exc
    assert last_error is not None
    raise last_error
