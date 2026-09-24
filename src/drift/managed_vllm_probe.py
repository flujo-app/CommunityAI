"""Bounded admission probe for one already supervised local vLLM instance.

Health/model/version HTTP responses are necessary evidence only. The process
owner must separately verify the executable, artifact, selected devices and
lifetime before publishing a provider route.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from drift.inference_provider import Availability
from drift.managed_vllm import BACKEND_VERSION, ManagedVllmBinding

_MAX_PROBE_BYTES = 8192


class ManagedVllmProbeError(RuntimeError):
    """The local backend did not match its proposed route binding."""


@dataclass(frozen=True)
class ManagedVllmProbe:
    model_id: str
    backend_version: str
    max_model_len: int
    checked_at_monotonic: float


async def probe_managed_vllm(
    binding: ManagedVllmBinding,
    *,
    expected_max_model_len: int,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    seconds: float = 5.0,
) -> ManagedVllmProbe:
    """Check health, exact vLLM version, single model and context under one deadline."""
    if type(binding) is not ManagedVllmBinding or binding.profile.availability is not Availability.AVAILABLE:
        raise ValueError("unavailable managed binding")
    if type(expected_max_model_len) is not int or not 1 <= expected_max_model_len <= 2048:
        raise ValueError("invalid expected context")
    if type(seconds) not in (int, float) or not 0 < seconds <= 30 or not callable(clock):
        raise ValueError("invalid probe deadline")
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise ValueError("invalid probe transport")
    deadline = clock() + seconds
    active_client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
    headers = {"Authorization": "Bearer " + binding.api_key}

    async def fetch(path: str) -> bytes:
        remaining = deadline - clock()
        if remaining <= 0:
            raise ManagedVllmProbeError("managed backend probe timed out")

        async def read() -> bytes:
            async with active_client.stream(
                "GET", binding.base_url + path, headers=headers, timeout=remaining, follow_redirects=False
            ) as response:
                if response.status_code != 200:
                    raise ManagedVllmProbeError("managed backend probe rejected")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_PROBE_BYTES:
                        raise ManagedVllmProbeError("managed backend probe response too large")
                return bytes(body)

        try:
            return await asyncio.wait_for(read(), remaining)
        except (TimeoutError, httpx.HTTPError) as exc:
            raise ManagedVllmProbeError("managed backend probe unavailable") from exc

    def document(body: bytes) -> dict:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagedVllmProbeError("invalid managed backend metadata") from exc
        if type(value) is not dict:
            raise ManagedVllmProbeError("invalid managed backend metadata")
        return value

    try:
        await fetch("/health")
        version = document(await fetch("/version"))
        if version.get("version") != BACKEND_VERSION:
            raise ManagedVllmProbeError("managed backend version mismatch")
        models = document(await fetch("/v1/models"))
        cards = models.get("data")
        if models.get("object") != "list" or type(cards) is not list or len(cards) != 1 or type(cards[0]) is not dict:
            raise ManagedVllmProbeError("managed backend model set mismatch")
        card = cards[0]
        if (
            card.get("id") != binding.served_model
            or type(card.get("max_model_len")) is not int
            or card["max_model_len"] != expected_max_model_len
        ):
            raise ManagedVllmProbeError("managed backend model or context mismatch")
        return ManagedVllmProbe(binding.served_model, BACKEND_VERSION, expected_max_model_len, clock())
    finally:
        await active_client.aclose()
