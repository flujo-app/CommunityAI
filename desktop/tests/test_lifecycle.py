from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import communityai_desktop.lifecycle as lifecycle
from communityai_desktop.client import NodeApiError
from communityai_desktop.credentials import CredentialProvision
from communityai_desktop.lifecycle import NodeLifecycleError, NodeLifecycleSupervisor

CONTROL_KEY = "drift_control_" + "L" * 43


class FakeStore:
    service = "test-service"
    account = "test-account"

    def __init__(self, source="generated", legacy_path=None):
        self.provisioned = CredentialProvision(CONTROL_KEY, source, legacy_path)
        self.retired = []

    def provision(self, path):
        return self.provisioned

    def retire_legacy_file(self, provision):
        self.retired.append(provision)
        return True


class FakeClient:
    def __init__(self, node_url, secret, *, timeout, error=None):
        self.node_url = node_url
        self.secret = secret
        self.timeout = timeout
        self.error = error
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        if self.error is not None:
            raise self.error
        return {"api_version": 1}


class FakeProcess:
    next_pid = 100

    def __init__(self, command, **kwargs):
        self.command = tuple(command)
        self.kwargs = kwargs
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


class PortSequence:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self, node_url, timeout):
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class NodeLifecycleTests(unittest.TestCase):
    def test_frozen_desktop_resolves_nested_node_sidecar(self):
        executable = Path.cwd() / "product" / ("CommunityAI.exe" if lifecycle.os.name == "nt" else "CommunityAI")
        suffix = ".exe" if lifecycle.os.name == "nt" else ""
        with mock.patch.object(lifecycle.sys, "frozen", True, create=True), mock.patch.object(
            lifecycle.sys, "executable", str(executable)
        ):
            command = lifecycle._default_node_command()

        self.assertEqual(
            command,
            (str(executable.resolve().parent / "node" / f"CommunityAI-Node{suffix}"),),
        )

    def _supervisor(self, directory, store, **kwargs):
        root = Path(directory)
        config = root / "node-config.json"
        config.write_text("{}\n", encoding="utf-8")
        return NodeLifecycleSupervisor(
            "http://127.0.0.1:8080",
            store,
            config_path=config,
            data_dir=root / "data",
            node_command=("python", "fake-node.py"),
            startup_timeout=2,
            poll_interval=0.1,
            **kwargs,
        )

    def test_reuses_an_authenticated_external_node_without_owning_it(self):
        processes = []
        clients = []

        def client_factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            clients.append(client)
            return client

        with TemporaryDirectory() as directory:
            supervisor = self._supervisor(
                directory,
                FakeStore(),
                process_factory=lambda *args, **kwargs: processes.append((args, kwargs)),
                client_factory=client_factory,
                port_probe=PortSequence(True),
            )
            returned = supervisor.ensure_client()
            supervisor.close()

        self.assertIs(returned, clients[0])
        self.assertEqual(returned.secret, CONTROL_KEY)
        self.assertEqual(processes, [])
        self.assertIsNone(supervisor.owned_pid)

    def test_starts_native_node_without_exposing_the_secret_and_retires_migration(self):
        processes = []
        clients = []
        clock = FakeClock()

        def process_factory(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            processes.append(process)
            return process

        def client_factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            clients.append(client)
            return client

        with TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "control-api.key"
            store = FakeStore("migrated", legacy_path)
            supervisor = self._supervisor(
                directory,
                store,
                process_factory=process_factory,
                client_factory=client_factory,
                port_probe=PortSequence(False, True),
                clock=clock,
                sleeper=clock.sleep,
            )
            returned = supervisor.ensure_client()
            owned_pid = supervisor.owned_pid
            supervisor.close()

        self.assertIs(returned, clients[0])
        self.assertIsNotNone(owned_pid)
        self.assertEqual(len(processes), 1)
        command = processes[0].command
        self.assertIn("--control_key_source", command)
        self.assertIn("native", command)
        self.assertEqual(command[command.index("--port") + 1], "8080")
        self.assertNotIn(CONTROL_KEY, command)
        self.assertNotIn(CONTROL_KEY, str(processes[0].kwargs))
        self.assertEqual(store.retired, [store.provisioned])
        self.assertTrue(processes[0].terminated)

    def test_does_not_replace_an_untrusted_service_on_the_configured_port(self):
        processes = []

        def client_factory(*args, **kwargs):
            return FakeClient(*args, **kwargs, error=NodeApiError(401, "no"))

        with TemporaryDirectory() as directory:
            supervisor = self._supervisor(
                directory,
                FakeStore(),
                process_factory=lambda *args, **kwargs: processes.append((args, kwargs)),
                client_factory=client_factory,
                port_probe=PortSequence(True),
            )
            with self.assertRaisesRegex(NodeLifecycleError, "Another local service"):
                supervisor.ensure_client()
            supervisor.close()

        self.assertEqual(processes, [])

    def test_crash_restarts_only_after_bounded_backoff(self):
        processes = []
        clock = FakeClock()

        def process_factory(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            processes.append(process)
            return process

        with TemporaryDirectory() as directory:
            supervisor = self._supervisor(
                directory,
                FakeStore(),
                process_factory=process_factory,
                client_factory=FakeClient,
                port_probe=PortSequence(False, True, False, False, True),
                clock=clock,
                sleeper=clock.sleep,
            )
            supervisor.ensure_client()
            processes[0].returncode = 7
            with self.assertRaisesRegex(NodeLifecycleError, "retry shortly"):
                supervisor.ensure_client()
            clock.value += 1.1
            supervisor.ensure_client()
            supervisor.close()

        self.assertEqual(len(processes), 2)
        self.assertTrue(processes[1].terminated)

    def test_missing_catalog_does_not_spawn_a_process(self):
        processes = []
        with TemporaryDirectory() as directory:
            supervisor = NodeLifecycleSupervisor(
                "http://127.0.0.1:8080",
                FakeStore(),
                config_path=Path(directory) / "missing.json",
                data_dir=Path(directory) / "data",
                node_command=("python", "fake-node.py"),
                process_factory=lambda *args, **kwargs: processes.append((args, kwargs)),
                client_factory=FakeClient,
                port_probe=PortSequence(False),
            )
            with self.assertRaisesRegex(NodeLifecycleError, "model catalog is not installed"):
                supervisor.ensure_client()
            supervisor.close()

        self.assertEqual(processes, [])


if __name__ == "__main__":
    unittest.main()
