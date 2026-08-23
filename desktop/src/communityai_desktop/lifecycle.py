"""Desktop ownership and supervision of the standalone CommunityAI node process."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlsplit

from communityai_desktop.client import NodeApiError, NodeClient, NodeClientError, normalize_loopback_url
from communityai_desktop.credentials import NativeCredentialStore

DEFAULT_NODE_DATA_DIR = Path.home() / ".drift" / "node"
DEFAULT_NODE_CONFIG_PATH = DEFAULT_NODE_DATA_DIR / "node-config.json"
PACKAGED_NODE_DIRECTORY = "node"
PACKAGED_NODE_NAME = "CommunityAI-Node"


class NodeLifecycleError(RuntimeError):
    """The desktop could not safely connect to or supervise its local node."""


def _default_node_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        suffix = ".exe" if os.name == "nt" else ""
        executable = Path(sys.executable).resolve().parent / PACKAGED_NODE_DIRECTORY / f"{PACKAGED_NODE_NAME}{suffix}"
        return (str(executable),)
    return (sys.executable, "-m", "drift.cli", "node")


def _port_is_open(node_url: str, timeout: float) -> bool:
    parsed = urlsplit(node_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class NodeLifecycleSupervisor:
    """Start one native-key node sidecar and stop only the process it owns."""

    def __init__(
        self,
        node_url: str,
        credential_store: NativeCredentialStore,
        *,
        config_path: Path | str = DEFAULT_NODE_CONFIG_PATH,
        data_dir: Path | str = DEFAULT_NODE_DATA_DIR,
        node_command: Optional[Sequence[str]] = None,
        startup_timeout: float = 45.0,
        poll_interval: float = 0.2,
        client_timeout: float = 2.0,
        process_factory: Callable[..., Any] = subprocess.Popen,
        client_factory: Callable[..., NodeClient] = NodeClient,
        port_probe: Callable[[str, float], bool] = _port_is_open,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.node_url = normalize_loopback_url(node_url)
        self.credential_store = credential_store
        self.config_path = Path(config_path).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.node_command = tuple(node_command or _default_node_command())
        if not self.node_command or any(not isinstance(part, str) or not part for part in self.node_command):
            raise ValueError("node command must contain non-empty strings")
        if startup_timeout <= 0 or poll_interval <= 0 or client_timeout <= 0:
            raise ValueError("node lifecycle timeouts must be positive")
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = float(poll_interval)
        self.client_timeout = float(client_timeout)
        self._process_factory = process_factory
        self._client_factory = client_factory
        self._port_probe = port_probe
        self._clock = clock
        self._sleeper = sleeper
        self._process = None
        self._closed = False
        self._closing = threading.Event()
        self._failures = 0
        self._next_start = 0.0
        self._lock = threading.RLock()

    @property
    def owned_pid(self) -> Optional[int]:
        with self._lock:
            process = self._process
            return process.pid if process is not None and process.poll() is None else None

    def _make_client(self, secret: str) -> NodeClient:
        return self._client_factory(self.node_url, secret, timeout=self.client_timeout)

    def _command(self) -> tuple[str, ...]:
        parsed = urlsplit(self.node_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        return (
            *self.node_command,
            "--config",
            str(self.config_path),
            "--data_dir",
            str(self.data_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--control_key_source",
            "native",
            "--credential_service",
            self.credential_store.service,
            "--credential_account",
            self.credential_store.account,
        )

    def _record_failure(self) -> None:
        self._failures += 1
        delay = min(30.0, float(2 ** min(self._failures - 1, 5)))
        self._next_start = self._clock() + delay

    def _start(self) -> None:
        if urlsplit(self.node_url).scheme != "http":
            raise NodeLifecycleError("Desktop-owned nodes require a loopback HTTP URL")
        if not self.config_path.is_file() or self.config_path.is_symlink():
            raise NodeLifecycleError(
                f"CommunityAI's model catalog is not installed yet ({self.config_path.name} is missing)"
            )
        executable = Path(self.node_command[0])
        if len(self.node_command) == 1 and not executable.is_file():
            raise NodeLifecycleError("The bundled CommunityAI node is missing; reinstall the application")
        if self._clock() < self._next_start:
            raise NodeLifecycleError("The local node stopped unexpectedly; CommunityAI will retry shortly")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(self.data_dir),
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self._process = self._process_factory(self._command(), **kwargs)
        except OSError as exc:
            self._record_failure()
            raise NodeLifecycleError(f"Could not start the bundled local node: {exc}") from exc

    def _stop_owned_process(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=5)
            except OSError:
                pass
        except OSError:
            pass

    def _wait_until_ready(self, client: NodeClient) -> None:
        deadline = self._clock() + self.startup_timeout
        last_error: Optional[Exception] = None
        while self._clock() < deadline:
            if self._closing.is_set():
                self._stop_owned_process()
                raise NodeLifecycleError("The local node supervisor is closing")
            process = self._process
            if process is None:
                raise NodeLifecycleError("The local node process was not started")
            exit_code = process.poll()
            if exit_code is not None:
                self._process = None
                self._record_failure()
                raise NodeLifecycleError(f"The local node stopped during startup (exit code {exit_code})")
            if self._port_probe(self.node_url, min(self.client_timeout, 0.25)):
                try:
                    client.status()
                    self._failures = 0
                    self._next_start = 0.0
                    return
                except NodeApiError as exc:
                    if exc.status_code in (401, 403):
                        self._stop_owned_process()
                        self._record_failure()
                        raise NodeLifecycleError("The local node rejected its native control credential") from exc
                    last_error = exc
                except NodeClientError as exc:
                    last_error = exc
            self._sleeper(self.poll_interval)

        self._stop_owned_process()
        self._record_failure()
        detail = f": {last_error}" if last_error is not None else ""
        raise NodeLifecycleError(f"The local node did not become ready in time{detail}")

    def ensure_client(self) -> NodeClient:
        """Return an authenticated client, starting or restarting the sidecar as needed."""
        with self._lock:
            if self._closed or self._closing.is_set():
                raise NodeLifecycleError("The local node supervisor is closed")
            provision = self.credential_store.provision(self.data_dir / "control-api.key")
            client = self._make_client(provision.secret)

            port_open = self._port_probe(self.node_url, min(self.client_timeout, 0.25))
            if port_open:
                try:
                    client.status()
                except NodeClientError as exc:
                    if self._process is None:
                        raise NodeLifecycleError(
                            "Another local service is using the CommunityAI port but did not accept its credential"
                        ) from exc
                    self._wait_until_ready(client)
                return client

            if self._process is not None:
                exit_code = self._process.poll()
                if exit_code is None:
                    self._wait_until_ready(client)
                    return client
                self._process = None
                self._record_failure()

            self._start()
            self._wait_until_ready(client)
            if provision.source == "migrated":
                self.credential_store.retire_legacy_file(provision)
            return client

    def close(self) -> None:
        self._closing.set()
        with self._lock:
            self._closed = True
            self._stop_owned_process()

    def __enter__(self) -> "NodeLifecycleSupervisor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.close()
