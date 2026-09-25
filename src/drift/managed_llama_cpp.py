"""Pinned llama.cpp OpenAI-stream adapter for synthetic local profiles.

The transport has been exercised with build b11173 on one local CUDA GPU.
This module does not launch or own the server, verify GGUF artifacts, or
qualify another model/device. Keep routes unavailable until those gates pass.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import httpx

from drift.inference_provider import Availability, ProviderProfile
from drift.managed_vllm import ManagedVllmAdapter

BACKEND_ID = "llama.cpp-b11173"


@dataclass(frozen=True)
class ManagedLlamaCppBinding:
    """Local deployment input, never inferred from a DHT or response frame."""

    profile: ProviderProfile
    served_model: str
    base_url: str
    api_key: str = field(repr=False)
    device_ids: tuple[int, ...]
    split_mode: str
    max_model_len: int
    backend_id: str = BACKEND_ID

    def __post_init__(self) -> None:
        if (
            type(self.profile) is not ProviderProfile
            or self.profile.availability is not Availability.AVAILABLE
            or type(self.served_model) is not str
            or self.served_model != self.profile.model_id
            or self.backend_id != BACKEND_ID
        ):
            raise ValueError("invalid llama.cpp profile or backend identity")
        endpoint = urlsplit(self.base_url)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {"127.0.0.1", "::1"}
            or endpoint.port is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query or endpoint.fragment
        ):
            raise ValueError("managed llama.cpp endpoint must be loopback HTTP")
        if (
            type(self.api_key) is not str or not self.api_key
            or any(ord(character) < 33 or ord(character) > 126 for character in self.api_key)
        ):
            raise ValueError("invalid managed llama.cpp API key")
        if (
            type(self.device_ids) is not tuple
            or not 1 <= len(self.device_ids) <= 32
            or any(type(device) is not int or not 0 <= device <= 255 for device in self.device_ids)
            or len(set(self.device_ids)) != len(self.device_ids)
        ):
            raise ValueError("invalid llama.cpp GPU selection")
        if (
            self.split_mode not in {"none", "layer", "tensor"}
            or (self.split_mode == "none") != (len(self.device_ids) == 1)
        ):
            raise ValueError("invalid llama.cpp split mode or GPU count")
        if type(self.max_model_len) is not int or not 1 <= self.max_model_len <= 2048:
            raise ValueError("invalid llama.cpp context envelope")


class ManagedLlamaCppAdapter(ManagedVllmAdapter):
    """Use the shared bounded OpenAI SSE validator with llama.cpp identity."""

    def __init__(
        self,
        binding: ManagedLlamaCppBinding,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_quarantine: Callable[[], None] | None = None,
        before_dispatch: Callable[[], bool] | None = None,
    ) -> None:
        if type(binding) is not ManagedLlamaCppBinding:
            raise ValueError("invalid llama.cpp adapter binding")
        self._initialize(binding, client, clock, on_quarantine, before_dispatch)
