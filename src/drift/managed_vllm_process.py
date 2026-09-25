"""Process ownership for a locally launched, synthetic-profile vLLM service.

This owns a process tree and a pinned entry point. It does not qualify weights,
hardware, licensing, or actual GPU release; those remain deployment gates.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

import psutil

from drift.inference_provider import Availability
from drift.managed_vllm import ManagedVllmBinding
from drift.node.edge_supervisor import _force_containment_exit, _new_containment


class ManagedVllmProcessError(RuntimeError):
    """The managed backend process could not be owned or fully stopped."""


class SupervisedLocalProcess:
    """One child tree with the same OS containment used by the edge benchmark."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._containment = None

    @property
    def running(self) -> bool:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return False
            try:
                return self._containment.has_members()
            except OSError:
                return False

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process is not None else None

    def owns_tcp_listener(self, host: str, port: int) -> bool:
        """Fail closed unless a tracked parent or descendant owns the endpoint."""
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return False
            try:
                root = psutil.Process(self._process.pid)
                for process in (root, *root.children(recursive=True)):
                    for connection in process.net_connections(kind="tcp"):
                        if (
                            connection.status == psutil.CONN_LISTEN
                            and connection.laddr
                            and connection.laddr.ip == host
                            and connection.laddr.port == port
                        ):
                            return True
            except (psutil.Error, OSError):
                return False
            return False

    def start(self, command: Sequence[str], environment: Mapping[str, str], *, cwd: str | Path) -> None:
        if (
            isinstance(command, (str, bytes))
            or not command
            or any(type(value) is not str or not value or "\0" in value for value in command)
            or not Path(command[0]).is_absolute()
            or not Path(command[0]).is_file()
            or not isinstance(environment, Mapping)
            or any(
                type(key) is not str
                or not key
                or "=" in key
                or "\0" in key
                or type(value) is not str
                or "\0" in value
                for key, value in environment.items()
            )
        ):
            raise ValueError("invalid supervised process input")
        directory = Path(cwd)
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("supervised working directory must exist")
        with self._lock:
            if self._process is not None:
                raise ManagedVllmProcessError("previous managed process still owns its containment")
            containment = _new_containment()
            process = None
            try:
                process = subprocess.Popen(
                    list(command),
                    cwd=str(directory),
                    env=dict(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    **containment.popen_kwargs(),
                )
                containment.attach(process)
                containment.resume(process)
            except (OSError, subprocess.SubprocessError) as exc:
                if process is not None:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                containment.close()
                raise ManagedVllmProcessError("could not launch and contain managed backend") from exc
            self._process = process
            self._containment = containment

    def stop(self) -> None:
        with self._lock:
            if self._process is None:
                return
            try:
                absent = _force_containment_exit(self._containment, self._process, 0.02)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ManagedVllmProcessError("managed backend stop is unconfirmed") from exc
            if not absent:
                raise ManagedVllmProcessError("managed backend process tree still owns resources")
            self._containment.close()
            self._containment = None
            self._process = None


class ManagedVllmProcessOwner:
    """Bind a verified local vLLM entry point to an explicit test-profile launch."""

    def __init__(
        self,
        binding: ManagedVllmBinding,
        *,
        model_directory: str | Path,
        executable: str | Path,
        executable_sha256: str,
        max_model_len: int,
    ) -> None:
        if type(binding) is not ManagedVllmBinding or binding.profile.availability is not Availability.AVAILABLE:
            raise ValueError("managed process requires an available synthetic profile")
        self.binding = binding
        self.model_directory = Path(model_directory)
        self.executable = Path(executable)
        if (
            not self.executable.is_absolute()
            or not self.executable.is_file()
            or type(executable_sha256) is not str
            or len(executable_sha256) != 64
            or any(character not in "0123456789abcdef" for character in executable_sha256)
        ):
            raise ValueError("managed executable requires an absolute path and SHA-256")
        self.executable_sha256 = executable_sha256
        self.max_model_len = max_model_len
        endpoint = urlsplit(binding.base_url)
        self._host = endpoint.hostname
        self._port = endpoint.port
        # Validate the complete immutable launch geometry before owning resources.
        binding.launch_spec(self.model_directory, max_model_len=max_model_len)
        self._process = SupervisedLocalProcess()
        self._probe_accepted = False

    @property
    def running(self) -> bool:
        return self._process.running

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def ready(self) -> bool:
        return self._probe_accepted and self.running and self._process.owns_tcp_listener(self._host, self._port)

    def start(self) -> None:
        self._probe_accepted = False
        digest = hashlib.sha256()
        with self.executable.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        current = digest.hexdigest()
        if current != self.executable_sha256:
            raise ManagedVllmProcessError("managed vLLM entry point hash mismatch")
        arguments, overrides = self.binding.launch_spec(self.model_directory, max_model_len=self.max_model_len)
        environment = os.environ.copy()
        environment.update(overrides)
        self._process.start(
            (str(self.executable.resolve(strict=True)), *arguments[1:]),
            environment,
            cwd=self.model_directory.resolve(strict=True),
        )

    async def probe(self, *, seconds: float = 5.0):
        """Admit only a live owned process with matching local HTTP metadata."""
        from drift.managed_vllm_probe import ManagedVllmProbeError, probe_managed_vllm

        if not self.running:
            raise ManagedVllmProcessError("managed backend is not running")
        try:
            if not self._process.owns_tcp_listener(self._host, self._port):
                raise ManagedVllmProbeError("managed process does not own its loopback listener")
            result = await probe_managed_vllm(
                self.binding, expected_max_model_len=self.max_model_len, seconds=seconds
            )
            if not self.running or not self._process.owns_tcp_listener(self._host, self._port):
                raise ManagedVllmProbeError("managed backend listener exited during admission")
        except Exception:
            self.stop()
            raise
        self._probe_accepted = True
        return result

    def new_adapter(self):
        """Create one admitted adapter whose failed stream tears down its tree."""
        from drift.managed_vllm import ManagedVllmAdapter

        if not self.ready:
            raise ManagedVllmProcessError("managed backend has not passed admission")
        return ManagedVllmAdapter(self.binding, on_quarantine=self.stop, before_dispatch=lambda: self.ready)

    def stop(self) -> None:
        self._probe_accepted = False
        self._process.stop()
