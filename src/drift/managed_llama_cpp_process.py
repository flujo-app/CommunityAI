"""Owned local llama.cpp process for one pinned synthetic GPU profile.

This is a single-GPU admission path. A successful probe does not qualify a
different GGUF, CUDA device, model family, or multi-GPU split.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from drift.managed_llama_cpp import ManagedLlamaCppAdapter, ManagedLlamaCppBinding
from drift.managed_vllm_process import ManagedVllmProcessError, SupervisedLocalProcess

_BUILD_INFO = "b11173-84e76d8a2"
_MAX_METADATA_BYTES = 128 * 1024


class ManagedLlamaCppProcessError(ManagedVllmProcessError):
    """Pinned llama.cpp process or admission could not be confirmed."""


@dataclass(frozen=True)
class ManagedLlamaCppProbe:
    model_id: str
    build_info: str
    max_model_len: int
    checked_at_monotonic: float


def _check_digest(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ManagedLlamaCppProcessError("pinned llama.cpp artifact hash mismatch")


class ManagedLlamaCppProcessOwner:
    """Own one verified llama-server, its key file, and its exact loopback route."""

    def __init__(
        self,
        binding: ManagedLlamaCppBinding,
        *,
        executable: str | Path,
        executable_sha256: str,
        model_file: str | Path,
        model_sha256: str,
    ) -> None:
        if type(binding) is not ManagedLlamaCppBinding or len(binding.device_ids) != 1:
            raise ValueError("only one qualified local llama.cpp GPU is admitted")
        self.binding = binding
        self.executable = Path(executable)
        self.model_file = Path(model_file)
        for path, digest in ((self.executable, executable_sha256), (self.model_file, model_sha256)):
            if (
                not path.is_absolute() or not path.is_file() or type(digest) is not str
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("managed llama.cpp artifact requires an absolute file and SHA-256")
        self.executable_sha256 = executable_sha256
        self.model_sha256 = model_sha256
        endpoint = urlsplit(binding.base_url)
        self._host = endpoint.hostname
        self._port = endpoint.port
        self._process = SupervisedLocalProcess()
        self._key_directory: tempfile.TemporaryDirectory | None = None
        self._probe_accepted = False

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def running(self) -> bool:
        return self._process.running

    @property
    def ready(self) -> bool:
        return (
            self._probe_accepted and self.running
            and self._process.owns_tcp_listener(self._host, self._port)
        )

    def start(self) -> None:
        if self.pid is not None:
            raise ManagedLlamaCppProcessError("previous llama.cpp process still owns its containment")
        self._probe_accepted = False
        _check_digest(self.executable, self.executable_sha256)
        _check_digest(self.model_file, self.model_sha256)
        version = subprocess.run(
            [str(self.executable), "--version"], capture_output=True, text=True,
            timeout=10, check=False,
        )
        if version.returncode or "build 11173, commit 84e76d8a2" not in version.stdout + version.stderr:
            # The hash is authoritative; version text also guards mismatched packaging.
            raise ManagedLlamaCppProcessError("llama.cpp build identity mismatch")
        key_directory = tempfile.TemporaryDirectory(prefix="communityai-llama-key-")
        key_file = Path(key_directory.name) / "key.txt"
        key_file.write_text(self.binding.api_key + "\n", encoding="ascii")
        command = (
            str(self.executable.resolve(strict=True)),
            "--model", str(self.model_file.resolve(strict=True)),
            "--alias", self.binding.served_model,
            "--host", self._host, "--port", str(self._port),
            "--api-key-file", str(key_file), "--no-ui", "--no-slots",
            "--device", "CUDA0", "--split-mode", "none", "--gpu-layers", "all",
            "--fit", "off", "--ctx-size", str(self.binding.max_model_len),
            "--parallel", "1", "--flash-attn", "off", "--no-cache-prompt",
        )
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LLAMA_ARG_")}
        environment.update(
            CUDA_VISIBLE_DEVICES=str(self.binding.device_ids[0]),
            HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
        )
        try:
            self._process.start(command, environment, cwd=self.executable.parent)
        except BaseException:
            key_directory.cleanup()
            raise
        self._key_directory = key_directory

    async def probe(self, *, seconds: float = 5.0) -> ManagedLlamaCppProbe:
        if type(seconds) not in (int, float) or not 0 < seconds <= 30:
            raise ValueError("invalid llama.cpp probe deadline")
        if not self.running:
            raise ManagedLlamaCppProcessError("llama.cpp process is not running")
        if not self._process.owns_tcp_listener(self._host, self._port):
            self.stop()
            raise ManagedLlamaCppProcessError("llama.cpp process does not own its loopback listener")
        deadline = time.monotonic() + seconds
        headers = {"Authorization": "Bearer " + self.binding.api_key}
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                async def fetch(path: str) -> dict:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ManagedLlamaCppProcessError("llama.cpp probe deadline")
                    async with client.stream(
                        "GET", self.binding.base_url + path, headers=headers, timeout=remaining,
                    ) as response:
                        if response.status_code != 200:
                            raise ManagedLlamaCppProcessError("llama.cpp probe rejected")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > _MAX_METADATA_BYTES:
                                raise ManagedLlamaCppProcessError("llama.cpp metadata exceeds bound")
                    try:
                        document = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ManagedLlamaCppProcessError("invalid llama.cpp metadata") from exc
                    if type(document) is not dict:
                        raise ManagedLlamaCppProcessError("invalid llama.cpp metadata")
                    return document

                await asyncio.wait_for(fetch("/health"), max(0.001, deadline - time.monotonic()))
                models = await asyncio.wait_for(fetch("/v1/models"), max(0.001, deadline - time.monotonic()))
                props = await asyncio.wait_for(fetch("/props"), max(0.001, deadline - time.monotonic()))
            cards = models.get("data")
            settings = props.get("default_generation_settings")
            if (
                models.get("object") != "list" or type(cards) is not list or len(cards) != 1
                or type(cards[0]) is not dict or cards[0].get("id") != self.binding.served_model
                or props.get("build_info") != _BUILD_INFO
                or props.get("model_path") != str(self.model_file.resolve(strict=True))
                or props.get("total_slots") != 1
                or type(settings) is not dict or settings.get("n_ctx") != self.binding.max_model_len
            ):
                raise ManagedLlamaCppProcessError("llama.cpp route metadata mismatch")
            if not self.running or not self._process.owns_tcp_listener(self._host, self._port):
                raise ManagedLlamaCppProcessError("llama.cpp listener exited during admission")
        except BaseException as exc:
            self.stop()
            if isinstance(exc, ManagedLlamaCppProcessError):
                raise
            raise ManagedLlamaCppProcessError("llama.cpp probe unavailable") from exc
        self._probe_accepted = True
        return ManagedLlamaCppProbe(
            self.binding.served_model, _BUILD_INFO, self.binding.max_model_len, time.monotonic(),
        )

    def new_adapter(self) -> ManagedLlamaCppAdapter:
        if not self.ready:
            raise ManagedLlamaCppProcessError("llama.cpp process has not passed admission")
        return ManagedLlamaCppAdapter(
            self.binding, on_quarantine=self.stop, before_dispatch=lambda: self.ready,
        )

    def stop(self) -> None:
        self._probe_accepted = False
        self._process.stop()
        if self._key_directory is not None:
            self._key_directory.cleanup()
            self._key_directory = None
