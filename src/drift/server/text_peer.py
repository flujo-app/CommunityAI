"""A contributor role for complete text inference through the existing block mesh."""

import asyncio
import contextlib
import threading
import time

from hivemind.utils import get_logger

from drift.node.loading import make_manifest_loader
from drift.server.text_generation import TextGenerationEngine
from drift.text_mesh import ANNOUNCEMENT_TTL, TextPeerProtocol, announcement_key, create_text_announcement

logger = get_logger(__name__)


class TextPeerService:
    def __init__(
        self,
        dht,
        identity,
        manifest,
        *,
        initial_peers,
        cache_dir,
        max_context_tokens=2048,
        max_output_tokens=512,
        request_timeout=900
    ):
        if dht.peer_id != identity.peer_id:
            raise ValueError("Text service must use its signing identity's peer transport")
        if not 1 <= max_output_tokens < max_context_tokens <= manifest.model.context_length:
            raise ValueError("Invalid text peer context/output limits")
        self.dht, self.identity, self.manifest = dht, identity, manifest
        self.initial_peers, self.cache_dir = initial_peers, cache_dir
        self.max_context_tokens, self.max_output_tokens = max_context_tokens, max_output_tokens
        self.request_timeout = request_timeout
        self._stop = threading.Event()
        self._thread = None
        self.engine = None
        self.error = None
        self.ready = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="community-text-peer", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self.engine is not None:
            self.engine.close()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _run(self):
        runtime = None
        try:
            runtime = make_manifest_loader(
                self.manifest,
                initial_peers=self.initial_peers,
                cache_dir=self.cache_dir,
                request_timeout=180,
                max_retries=1,
            )()
            if not self._stop.is_set():
                # The tensor client normally starts discovery on first inference.
                # Text peers must establish readiness before admitting that request.
                runtime.model.transformer.h.sequence_manager.start_discovery()
                self.engine = TextGenerationEngine(
                    runtime,
                    max_context_tokens=self.max_context_tokens,
                    max_output_tokens=self.max_output_tokens,
                    request_timeout=self.request_timeout,
                )
                asyncio.run(self._serve(runtime))
        except Exception as exc:
            self.error = type(exc).__name__
            logger.exception("Community text peer could not start")
        finally:
            self.ready.clear()
            if runtime is not None and runtime.close is not None:
                runtime.close()

    async def _serve(self, runtime):
        p2p = await self.dht.replicate_p2p()
        protocol = TextPeerProtocol(self.manifest, self.engine)
        try:
            await protocol.add_p2p_handlers(p2p)
            while not self._stop.is_set():
                health = runtime.route_health()
                complete = (
                    health.get("status") == "complete"
                    and health.get("covered_blocks") == self.manifest.model.num_blocks
                )
                if complete:
                    record = create_text_announcement(
                        self.manifest,
                        self.identity,
                        max_context_tokens=self.max_context_tokens,
                        max_output_tokens=self.max_output_tokens,
                    )
                    await asyncio.to_thread(
                        self.dht.store,
                        announcement_key(self.manifest),
                        record.to_dict(),
                        subkey=str(self.identity.peer_id),
                        expiration_time=time.time() + ANNOUNCEMENT_TTL,
                    )
                    self.ready.set()
                else:
                    self.ready.clear()
                    await asyncio.to_thread(
                        self.dht.store,
                        announcement_key(self.manifest),
                        None,
                        subkey=str(self.identity.peer_id),
                        expiration_time=time.time() + ANNOUNCEMENT_TTL,
                    )
                await asyncio.to_thread(self._stop.wait, 10)
        finally:
            self.ready.clear()
            self.engine.close()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.dht.store,
                    announcement_key(self.manifest),
                    None,
                    subkey=str(self.identity.peer_id),
                    expiration_time=time.time() + ANNOUNCEMENT_TTL,
                )
            await protocol.remove_p2p_handlers(p2p)
            await p2p.shutdown()
