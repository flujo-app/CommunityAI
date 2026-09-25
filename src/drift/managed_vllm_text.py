"""Synthetic-profile bridge from the current OpenAI API to managed vLLM.

Registration remains an explicit local operation.  This bridge has no DHT
advertisement, model qualification authority, or billing role. A reviewed
chat encoder may be supplied for a synthetic profile.
"""

from __future__ import annotations

from typing import AsyncIterator, Callable

from drift.inference_provider import (
    MAX_REQUEST_BYTES,
    Availability,
    EventKind,
    InferenceLimits,
    InferenceRequest,
    ProviderIdentity,
)
from drift.managed_vllm import ManagedGenerationOptions, ManagedVllmAdapter, ManagedVllmError
from drift.text_request import RequestContext


class ManagedProviderUnavailable(TimeoutError):
    """A managed test provider did not produce an accepted answer."""


class ManagedVllmTextClient:
    """Match ``TextPeerClient.stream`` without changing the legacy wire path."""

    supports_request_context = True

    def __init__(
        self,
        adapter: ManagedVllmAdapter,
        identity: ProviderIdentity,
        manifest_digest: str,
        *,
        chat_encoder: Callable[[list[dict]], str] | None = None,
        chat_thinking: bool = False,
    ) -> None:
        if type(adapter) is not ManagedVllmAdapter or type(identity) is not ProviderIdentity:
            raise ValueError("invalid managed provider identity")
        if type(manifest_digest) is not str or not manifest_digest.startswith("sha256:") or len(manifest_digest) != 71:
            raise ValueError("invalid managed manifest digest")
        try:
            int(manifest_digest[7:], 16)
        except ValueError as exc:
            raise ValueError("invalid managed manifest digest") from exc
        self.adapter = adapter
        self.identity = identity
        self.manifest_digest = manifest_digest
        if (chat_encoder is not None and not callable(chat_encoder)) or type(chat_thinking) is not bool:
            raise ValueError("invalid managed chat encoder")
        if chat_encoder is None and chat_thinking:
            raise ValueError("thinking mode requires a managed chat encoder")
        self.chat_encoder = chat_encoder
        self.chat_thinking = chat_thinking

    async def stream(self, body: dict, *, chat: bool, context: RequestContext) -> AsyncIterator[dict]:
        if type(context) is not RequestContext or type(body) is not dict:
            raise ValueError("invalid managed request context")
        if body.get("model") != self.manifest_digest:
            raise ValueError("managed manifest identity mismatch")
        if self.adapter.binding.profile.availability is not Availability.AVAILABLE:
            raise ManagedProviderUnavailable("managed profile unavailable")
        if chat:
            if self.chat_encoder is None:
                raise ValueError("managed chat requires a qualified prompt encoder")
            if body.get("enable_thinking") is not self.chat_thinking:
                raise ValueError("managed chat requires an explicit qualified thinking mode")
            prompt = self.chat_encoder(body.get("messages"))
        else:
            prompt = body.get("prompt")
        if type(prompt) is not str or not prompt:
            raise ValueError("managed completions require one text prompt")
        try:
            prompt_bytes = prompt.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid managed prompt") from exc
        if len(prompt_bytes) > MAX_REQUEST_BYTES:
            raise ValueError("managed prompt exceeds request limit")
        maximum = body.get("max_tokens")
        if chat:
            alternate = body.get("max_completion_tokens")
            if maximum is not None and alternate is not None and maximum != alternate:
                raise ValueError("conflicting managed output limits")
            maximum = alternate if maximum is None else maximum
        maximum = 512 if maximum is None else maximum
        if type(maximum) is not int or not 1 <= maximum <= 512:
            raise ValueError("managed output limit must be 1..512")
        stop = body.get("stop")
        stop = () if stop is None else ((stop,) if type(stop) is str else stop)
        if type(stop) is list:
            stop = tuple(stop)
        options = ManagedGenerationOptions(
            temperature=1.0 if body.get("temperature") is None else body["temperature"],
            top_p=1.0 if body.get("top_p") is None else body["top_p"],
            stop=stop,
        )
        context.require_live()
        profile = self.adapter.binding.profile
        request = InferenceRequest(
            self.identity,
            profile.profile_id,
            profile.model_id,
            context.request_id,
            context.attempt_id,
            context.issued_at,
            context.deadline,
            prompt,
            InferenceLimits(2048 - maximum, maximum, 4096, 1 << 20),
        )
        iterator = self.adapter.stream(request, options=options)
        terminal = False
        try:
            async for event in iterator:
                if event.kind is EventKind.STARTED:
                    yield {"type": "heartbeat"}
                elif event.kind is EventKind.OUTPUT:
                    yield {"type": "delta", "text": event.text}
                elif event.kind is EventKind.COMPLETED:
                    if event.finish_reason not in {"stop", "length"}:
                        raise ManagedProviderUnavailable("managed completion reason missing")
                    terminal = True
                    yield {
                        "type": "done",
                        "finish_reason": event.finish_reason,
                        "usage": {
                            "prompt_tokens": event.usage.input_units,
                            "completion_tokens": event.usage.output_units,
                            "total_tokens": event.usage.total_units,
                        },
                    }
                elif event.kind in {EventKind.REFUSED, EventKind.FAILED, EventKind.STOPPED}:
                    terminal = True
                    raise ManagedProviderUnavailable("managed provider unavailable")
                else:
                    raise ManagedProviderUnavailable("invalid managed provider event")
            if not terminal:
                raise ManagedProviderUnavailable("managed provider stream ended without completion")
        except ManagedVllmError as exc:
            raise ManagedProviderUnavailable(str(exc)) from exc
        finally:
            await iterator.aclose()
