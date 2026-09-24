"""Bounded local adapter for a managed vLLM OpenAI completions server.

Only synthetic ``test/*`` profiles can currently be AVAILABLE under the
provider contract.  This adapter does not launch vLLM, qualify a model/GPU,
authenticate remote worker identity, or prove server-side stop on disconnect.
Keep its HTTP endpoint on loopback behind the managed deployment boundary.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

import httpx

from drift.inference_provider import (
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    Availability,
    EventKind,
    InferenceRequest,
    ProviderEvent,
    ProviderProfile,
    ProviderStreamValidator,
    Usage,
    refusal_for,
)

_MAX_SSE_FRAME = MAX_EVENT_BYTES + 8192
BACKEND_VERSION = "0.30.0"
_BACKEND_ID = f"vllm-v{BACKEND_VERSION}"


class ManagedVllmError(RuntimeError):
    """The managed backend did not produce an accepted completion."""


class ManagedVllmStopUnconfirmed(ManagedVllmError):
    """The connection closed without proof that backend work stopped."""


@dataclass(frozen=True)
class ManagedGenerationOptions:
    temperature: float = 1.0
    top_p: float = 1.0
    stop: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.temperature) not in (int, float) or not 0 <= self.temperature <= 2:
            raise ValueError("invalid managed temperature")
        if type(self.top_p) not in (int, float) or not 0 < self.top_p <= 1:
            raise ValueError("invalid managed top_p")
        if type(self.stop) is not tuple or len(self.stop) > 4:
            raise ValueError("invalid managed stop set")
        for item in self.stop:
            if type(item) is not str or not item:
                raise ValueError("invalid managed stop string")
            try:
                size = len(item.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("invalid managed stop string") from exc
            if size > 256:
                raise ValueError("invalid managed stop string")


@dataclass(frozen=True)
class ManagedVllmBinding:
    """Local deployment input, never derived from DHT or a response frame."""

    profile: ProviderProfile
    served_model: str
    base_url: str
    api_key: str = field(repr=False)
    device_ids: tuple[int, ...]
    tensor_parallel_size: int
    pipeline_parallel_size: int
    backend_id: str = _BACKEND_ID

    def __post_init__(self) -> None:
        if type(self.profile) is not ProviderProfile or type(self.served_model) is not str:
            raise ValueError("invalid profile binding")
        if self.served_model != self.profile.model_id or self.backend_id != _BACKEND_ID:
            raise ValueError("model or backend identity mismatch")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("managed vLLM endpoint must be explicit loopback HTTP")
        if (
            type(self.api_key) is not str
            or not self.api_key
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in self.api_key)
        ):
            raise ValueError("invalid managed API key")
        if type(self.device_ids) is not tuple or not self.device_ids or len(self.device_ids) > 32:
            raise ValueError("invalid GPU selection")
        if any(type(device) is not int or device < 0 or device > 255 for device in self.device_ids):
            raise ValueError("invalid GPU selection")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("duplicate GPU selection")
        if type(self.tensor_parallel_size) is not int or type(self.pipeline_parallel_size) is not int:
            raise ValueError("invalid parallel geometry")
        if self.tensor_parallel_size < 1 or self.pipeline_parallel_size < 1:
            raise ValueError("invalid parallel geometry")
        if self.tensor_parallel_size * self.pipeline_parallel_size != len(self.device_ids):
            raise ValueError("parallel geometry does not match selected GPUs")

    def launch_spec(self, model_directory: str | Path, *, max_model_len: int) -> tuple[tuple[str, ...], dict[str, str]]:
        """Build a single-host command for an already qualified test profile.

        The caller still owns artifact verification, process supervision, device
        identity checks, and admission.  No shell interpolation is involved.
        """
        if self.profile.availability is not Availability.AVAILABLE:
            raise ValueError("unavailable profile cannot be launched")
        path = Path(model_directory)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("model directory must exist locally")
        if type(max_model_len) is not int or not 1 <= max_model_len <= 2048:
            raise ValueError("unqualified context envelope")
        parsed = urlsplit(self.base_url)
        command = (
            "vllm",
            "serve",
            str(path.resolve(strict=True)),
            "--served-model-name",
            self.served_model,
            "--host",
            parsed.hostname,
            "--port",
            str(parsed.port),
            "--tensor-parallel-size",
            str(self.tensor_parallel_size),
            "--pipeline-parallel-size",
            str(self.pipeline_parallel_size),
            "--distributed-executor-backend",
            "mp",
            "--max-model-len",
            str(max_model_len),
            "--no-enable-log-requests",
        )
        environment = {
            "CUDA_VISIBLE_DEVICES": ",".join(str(device) for device in self.device_ids),
            "VLLM_API_KEY": self.api_key,
        }
        return command, environment


def _sse_data(frame: bytes) -> bytes | None:
    lines = frame.replace(b"\r\n", b"\n").split(b"\n")
    data = []
    for line in lines:
        if not line or line.startswith(b":"):
            continue
        if line.startswith(b"data: "):
            data.append(line[6:])
        else:
            raise ManagedVllmError("unsupported SSE frame")
    if not data:
        return None
    if len(data) != 1:
        raise ManagedVllmError("multi-line SSE data is unsupported")
    return data[0]


async def _bounded_sse(response: httpx.Response, deadline: float, clock: Callable[[], float]) -> AsyncIterator[bytes]:
    buffer = bytearray()
    frame_count = 0
    chunks = response.aiter_bytes(chunk_size=64 * 1024)
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("backend deadline")
        try:
            chunk = await asyncio.wait_for(anext(chunks), remaining)
        except StopAsyncIteration:
            break
        buffer.extend(chunk)
        while True:
            data = bytes(buffer)
            normalized = data.replace(b"\r\n", b"\n")
            end = normalized.find(b"\n\n")
            if end < 0:
                if len(buffer) > _MAX_SSE_FRAME:
                    raise ManagedVllmError("SSE frame exceeds bound")
                break
            if end > _MAX_SSE_FRAME:
                raise ManagedVllmError("SSE frame exceeds bound")
            frame_count += 1
            if frame_count > MAX_EVENTS * 2:
                raise ManagedVllmError("too many SSE frames")
            frame = normalized[:end]
            buffer[:] = normalized[end + 2 :]
            payload = _sse_data(frame)
            if payload is not None:
                yield payload
    if buffer:
        raise ManagedVllmError("truncated SSE response")


class ManagedVllmAdapter:
    """Validate one local managed stream against the typed provider contract."""

    def __init__(
        self,
        binding: ManagedVllmBinding,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(binding) is not ManagedVllmBinding or not callable(clock):
            raise ValueError("invalid adapter input")
        self.binding = binding
        self._client = client
        self._clock = clock

    async def stream(
        self, request: InferenceRequest, *, options: ManagedGenerationOptions | None = None
    ) -> AsyncIterator[ProviderEvent]:
        if type(request) is not InferenceRequest:
            raise ValueError("invalid request")
        if options is None:
            options = ManagedGenerationOptions()
        if type(options) is not ManagedGenerationOptions:
            raise ValueError("invalid generation options")
        profile = self.binding.profile
        validator = ProviderStreamValidator(profile, request, clock=self._clock)
        sequence = 0

        def event(kind: EventKind, **fields: object) -> ProviderEvent:
            nonlocal sequence
            result = ProviderEvent(
                request.identity,
                request.profile_id,
                request.model_id,
                request.request_id,
                request.attempt_id,
                sequence,
                kind,
                **fields,
            )
            validator.accept(result)
            sequence += 1
            return result

        refusal = refusal_for(profile, request)
        if refusal is not None:
            yield event(EventKind.REFUSED, refusal=refusal)
            return
        remaining = request.deadline - self._clock()
        if remaining <= 0:
            raise ManagedVllmError("deadline before dispatch")
        if profile.availability is not Availability.AVAILABLE:
            raise ManagedVllmError("profile unavailable")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(base_url=self.binding.base_url, trust_env=False)
        payload = {
            "model": self.binding.served_model,
            "prompt": request.prompt,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": request.limits.max_output_units,
            "n": 1,
            "temperature": options.temperature,
            "top_p": options.top_p,
        }
        if options.stop:
            payload["stop"] = list(options.stop)
        headers = {"Authorization": "Bearer " + self.binding.api_key, "Accept": "text/event-stream"}
        response_id = None
        saw_finish = False
        finish_reason = None
        usage = None
        saw_done = False
        try:
            stream_context = client.stream(
                "POST",
                self.binding.base_url + "/v1/completions",
                json=payload,
                headers=headers,
                timeout=remaining,
            )
            response = await asyncio.wait_for(stream_context.__aenter__(), remaining)
            try:
                if response.status_code != 200:
                    yield event(EventKind.FAILED, failure_code="backend_http_error")
                    return
                yield event(EventKind.STARTED)
                async for raw in _bounded_sse(response, request.deadline, self._clock):
                    if saw_done:
                        raise ManagedVllmError("data after backend terminator")
                    if raw == b"[DONE]":
                        saw_done = True
                        continue
                    try:
                        frame = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ManagedVllmError("malformed backend JSON") from exc
                    if type(frame) is not dict or frame.get("model") != self.binding.served_model:
                        raise ManagedVllmError("backend model identity mismatch")
                    frame_id = frame.get("id")
                    if type(frame_id) is not str or not frame_id or len(frame_id) > 192:
                        raise ManagedVllmError("invalid backend response ID")
                    if response_id is None:
                        response_id = frame_id
                    elif response_id != frame_id:
                        raise ManagedVllmError("backend response ID changed")
                    choices = frame.get("choices")
                    if type(choices) is not list or len(choices) > 1:
                        raise ManagedVllmError("invalid backend choices")
                    if choices:
                        choice = choices[0]
                        if type(choice) is not dict or choice.get("index") != 0:
                            raise ManagedVllmError("invalid backend choice")
                        output = choice.get("text")
                        finish = choice.get("finish_reason")
                        if type(output) is not str or (finish is not None and finish not in {"stop", "length"}):
                            raise ManagedVllmError("unsupported backend output")
                        if usage is not None or (saw_finish and finish is not None):
                            raise ManagedVllmError("backend continued after final chunk")
                        if saw_finish and output:
                            raise ManagedVllmError("output after finish")
                        if output:
                            yield event(EventKind.OUTPUT, text=output)
                        if finish is not None:
                            saw_finish = True
                            finish_reason = finish
                    reported = frame.get("usage")
                    if reported is not None:
                        if type(reported) is not dict or usage is not None or not saw_finish:
                            raise ManagedVllmError("invalid backend usage")
                        usage = Usage(
                            reported.get("prompt_tokens"),
                            reported.get("completion_tokens"),
                            reported.get("total_tokens"),
                        )
            finally:
                try:
                    await asyncio.wait_for(stream_context.__aexit__(None, None, None), 3.0)
                except TimeoutError as exc:
                    raise ManagedVllmStopUnconfirmed("backend close timed out; stop unconfirmed") from exc
            if not saw_done or not saw_finish or usage is None:
                raise ManagedVllmError("backend stream ended without completion and usage")
            yield event(EventKind.COMPLETED, usage=usage, finish_reason=finish_reason)
        except ManagedVllmStopUnconfirmed:
            raise
        except TimeoutError as exc:
            validator.enforce_deadline()
            raise ManagedVllmStopUnconfirmed("backend deadline; stop unconfirmed") from exc
        except (httpx.HTTPError, ManagedVllmError, ValueError) as exc:
            if self._clock() >= request.deadline:
                validator.enforce_deadline()
                raise ManagedVllmStopUnconfirmed("backend deadline; stop unconfirmed") from exc
            yield event(EventKind.FAILED, failure_code="backend_stream_error")
        finally:
            if owns_client:
                await client.aclose()
