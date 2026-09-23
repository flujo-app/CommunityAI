from __future__ import annotations

import builtins
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from communityai_desktop.profiles import VolunteerProfile

DESKTOP = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load("volunteer_node_launcher_test", DESKTOP / "launch_volunteer_node.py")
generic = _load("generic_node_launcher_test", DESKTOP / "launch_node.py")


class VolunteerNodeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.profile = VolunteerProfile(self.home / ".communityai" / "multigpu-volunteer")
        self.environment = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.dispatches = []

    def _runtime_module(self, mode):
        module = types.ModuleType(f"drift.cli.run_{mode}")

        def run():
            self.dispatches.append((list(sys.argv), Path.cwd(), os.environ.copy()))

        module.main = run
        return module

    def _run(self, arguments, *, entry_validator=None):
        # These are argument/environment unit fixtures. The actual Linux
        # service/generation admission path is exercised by native tests.
        entry = types.ModuleType("drift.node.linux_anchor_entry")
        entry.NODE_TOKEN_ENV = "COMMUNITYAI_ANCHOR_NODE_TOKEN"
        entry.validate_node_entry = entry_validator or (lambda *args: None)
        runtime_modules = {
            "launch_node": generic,
            "drift.cli.run_node": self._runtime_module("node"),
            "drift.cli.run_bootstrap": self._runtime_module("bootstrap"),
            "drift.cli.run_server": self._runtime_module("server"),
            "drift.cli.run_edge_acquisition": self._runtime_module("edge_acquisition"),
            "drift.node.linux_anchor_entry": entry,
        }
        with patch.dict(sys.modules, runtime_modules):
            return launcher.main(arguments)

    def _worker(self):
        self.profile.prepare()
        manifest = self.profile.data_dir / "manifests" / "model.json"
        manifest.parent.mkdir()
        manifest.write_text("{}", encoding="utf-8")
        self.manifest = manifest
        os.environ[launcher.PARENT_PID_ENV] = str(os.getppid())
        os.environ[launcher.PROFILE_ROOT_ENV] = str(self.profile.root)
        return [
            "server",
            "test/model",
            "--model_manifest",
            str(manifest),
            "--identity_path",
            str(self.profile.data_dir / "worker-identities" / "gpu-0.key"),
            "--initial_peers",
            "/ip4/127.0.0.1/tcp/31337/p2p/test",
            "--throughput",
            "1.0",
            "--block_indices",
            "0:2",
            "--device",
            "cuda:0",
            "--max_processing_percent",
            "50.0",
        ]

    def test_node_forces_fixed_arguments_before_real_generic_dispatch(self):
        ordinary = self.home / ".drift" / "node" / "identity.key"
        ordinary.parent.mkdir(parents=True)
        ordinary.write_bytes(b"ordinary identity")
        os.environ.update(
            {
                "HF_TOKEN": "inherited",
                "HUGGING_FACE_HUB_TOKEN": "inherited",
                "HUGGINGFACEHUB_API_TOKEN": "inherited",
                "DRIFT_DOWNLOAD_PROGRESS": str(ordinary),
            }
        )
        self.assertEqual(self._run([]), 0)
        arguments, directory, environment = self.dispatches[0]
        self.assertEqual(
            arguments[1:],
            [
                "--config",
                str(self.profile.config_path),
                "--data_dir",
                str(self.profile.data_dir),
                "--host",
                "127.0.0.1",
                "--port",
                "18081",
                "--control_key_source",
                "native",
                "--credential_service",
                self.profile.credential_service,
                "--credential_account",
                self.profile.credential_account,
                "--pause_sharing_on_start",
                "--local_inference_cpu_only",
            ],
        )
        self.assertEqual(directory, self.profile.data_dir)
        self.assertEqual(environment[launcher.PARENT_PID_ENV], str(os.getpid()))
        self.assertEqual(environment[launcher.PROFILE_ROOT_ENV], str(self.profile.root))
        for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN", "DRIFT_DOWNLOAD_PROGRESS"):
            self.assertNotIn(key, environment)
        for key in ("HF_HOME", "HF_TOKEN_PATH", "DRIFT_CACHE", "TORCH_HOME", "TMP", "TEMP", "TMPDIR"):
            self.assertTrue(Path(environment[key]).is_relative_to(self.profile.root))
        self.assertEqual(ordinary.read_bytes(), b"ordinary identity")
        self.assertEqual(list(ordinary.parent.iterdir()), [ordinary])

    def test_anchor_is_an_exact_no_argument_dispatch_without_node_or_profile_mutation(self):
        module = types.ModuleType("drift.node.linux_anchor")
        calls = []
        module.serve_anchor = lambda **kwargs: calls.append(kwargs["controller_factory"]) or 0
        with patch.dict(sys.modules, {"drift.node.linux_anchor": module}):
            for mode in ("anchor", "anchor-initialize"):
                self.assertEqual(launcher.main([mode]), 0)
                for extra in (["--profile", "other"], ["--worker-cgroup-root", "/other"], ["--help"], ["shell"]):
                    with self.assertRaises(ValueError):
                        launcher.main([mode, *extra])
        with patch.object(launcher, "_anchor_controller", return_value="owner") as factory:
            self.assertEqual(calls[0]("layout"), "owner")
            self.assertEqual(calls[1]("layout"), "owner")
        self.assertEqual(factory.call_args_list[0].kwargs, {"initialize": False})
        self.assertEqual(factory.call_args_list[1].kwargs, {"initialize": True})
        self.assertFalse(self.profile.root.exists())
        self.assertEqual(self.dispatches, [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "real Linux directory fsync")
    def test_partial_bootstrap_is_retained_and_both_start_modes_refuse(self):
        from drift.node import linux_anchor, linux_anchor_node, linux_anchor_state

        executable = self.home / "CommunityAI-Node"
        executable.write_bytes(b"fixture, not a qualified frozen artifact")
        layout = types.SimpleNamespace(validate=lambda: None, service=types.SimpleNamespace(pid=os.getpid()))
        sync = linux_anchor_state._sync_directory
        synced = []

        def fail_after_root(path, identity):
            sync(path, identity)
            synced.append(path)
            if path == self.profile.root.parent:
                raise OSError("fixture interrupted after empty profile creation")

        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)):
            with patch.object(linux_anchor_state, "_sync_directory", side_effect=fail_after_root):
                with self.assertRaises(OSError):
                    launcher._anchor_controller(layout, initialize=True)
            self.assertEqual(list(self.profile.root.iterdir()), [])
            self.assertIn(self.home, synced)
            with self.assertRaises(FileExistsError):
                launcher._anchor_controller(layout, initialize=True)
            with self.assertRaises(linux_anchor.RecoverableStateError):
                launcher._anchor_controller(layout)
            self.assertEqual(list(self.profile.root.iterdir()), [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "real Linux directory fsync")
    def test_fixed_frozen_factory_requires_a_new_profile_and_exact_command(self):
        from drift.node import linux_anchor_node

        executable = self.home / "CommunityAI-Node"
        executable.write_bytes(b"fixture, not a qualified frozen artifact")
        layout = types.SimpleNamespace(validate=lambda: None)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)):
            with patch.object(linux_anchor_node, "AnchorNode", return_value="controller") as owner:
                self.assertEqual(launcher._anchor_controller(layout, initialize=True), "controller")
                factory = owner.call_args.args[2]
                self.assertTrue(owner.call_args.kwargs["initialize"])
                command, env, cwd = factory("/fixture/workers")
                self.assertEqual(command[0], str(executable))
                self.assertEqual(command[-2:], ["--worker-cgroup-root", "/fixture/workers"])
                self.assertEqual(cwd, str(self.profile.data_dir))
                self.assertNotIn(launcher.PARENT_PID_ENV, env)
            with self.assertRaises(FileExistsError):
                launcher._anchor_controller(layout, initialize=True)

    def test_linux_node_rejects_missing_anchor_before_profile_or_runtime_mutation(self):
        seen = []

        def rejected(*args):
            seen.append(args)
            raise RuntimeError("unqualified anchor")

        with patch.object(sys, "platform", "linux"), self.assertRaisesRegex(RuntimeError, "unqualified anchor"):
            self._run([], entry_validator=rejected)
        self.assertEqual(seen, [(self.profile.root, None, None)])
        self.assertFalse(self.profile.root.exists())
        self.assertEqual(self.dispatches, [])

    def test_linux_node_consumes_exact_birth_token_before_dispatch(self):
        seen = []
        os.environ["COMMUNITYAI_ANCHOR_NODE_TOKEN"] = "a" * 32
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(
                self._run(
                    ["--worker-cgroup-root", "/fixture/workers"], entry_validator=lambda *args: seen.append(args)
                ),
                0,
            )
        self.assertEqual(seen, [(self.profile.root, "a" * 32, "/fixture/workers")])
        self.assertNotIn("COMMUNITYAI_ANCHOR_NODE_TOKEN", self.dispatches[0][2])
        self.assertIn("--pause_sharing_on_start", self.dispatches[0][0])

    def test_explicit_cgroup_root_is_forwarded_without_widening_the_fixed_node_profile(self):
        root = "/delegated/communityai-volunteer"
        for arguments in (["--worker-cgroup-root", root], ["--worker-cgroup-root=" + root]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self._run(arguments), 0)
                forwarded, directory, environment = self.dispatches.pop()
                self.assertEqual(forwarded[-2:], ["--worker-cgroup-root", root])
                self.assertEqual(forwarded.count("--worker-cgroup-root"), 1)
                self.assertIn("--pause_sharing_on_start", forwarded)
                self.assertIn("--local_inference_cpu_only", forwarded)
                self.assertEqual(forwarded[forwarded.index("--port") + 1], "18081")
                self.assertEqual(directory, self.profile.data_dir)
                self.assertEqual(environment[launcher.PROFILE_ROOT_ENV], str(self.profile.root))

    def test_invalid_or_ambiguous_cgroup_root_is_rejected_before_profile_creation_or_dispatch(self):
        values = (
            "",
            "relative/root",
            "/delegated/../other",
            "//delegated/root",
            "/delegated//root",
            "C:\\root",
            "/root\\child",
            "/root\x00child",
        )
        cases = [["--worker-cgroup-root", value] for value in values]
        cases.extend(
            (
                ["--worker-cgroup-root"],
                ["--worker-cgroup-root", "/one", "--worker-cgroup-root=/two"],
                ["--worker-cgroup", "/one"],
                ["--worker-cgroup-root=--config"],
            )
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self._run(arguments)
            self.assertEqual(self.dispatches, [])
            self.assertFalse(self.profile.root.exists())

    def test_cgroup_root_is_not_a_worker_bootstrap_or_diagnostic_option(self):
        arguments = self._worker()
        bootstrap = self.home / "catalog-bootstrap.json"
        bootstrap.write_text("{}", encoding="utf-8")
        for command in (arguments, ["bootstrap", str(bootstrap)], ["--self-test"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self._run([*command, "--worker-cgroup-root", "/delegated/volunteer"])
        self.assertEqual(self.dispatches, [])

    def test_identical_gui_arguments_are_accepted_and_duplicates_or_overrides_rejected(self):
        self._run([])
        canonical = self.dispatches.pop()[0][1:]
        self._run(canonical)
        self.assertEqual(self.dispatches.pop()[0][1:], canonical)
        cases = [
            ["--port", "8080"],
            ["--host", "0.0.0.0"],
            ["--control_key_source", "file"],
            ["--credential_service", "ordinary"],
            ["--credential_account", "ordinary"],
            ["--config", str(self.home / ".drift" / "node-config.json")],
            ["--data_dir", str(self.home / ".drift")],
            ["--api_key", "anything"],
            ["--token", "anything"],
            ["--profile", "standard"],
            ["--conf", str(self.profile.config_path)],
            ["--port", "18081", "--port=18081"],
            ["--pause_sharing_on_start=false"],
            ["--local_inference_cpu_only", "--local_inference_cpu_only"],
            ["outside-model.json"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self._run(arguments)
        self.assertEqual(self.dispatches, [])

    def test_freeze_support_precedes_profile_import_and_private_environment_precedes_runtime_import(self):
        events = []
        real_import = builtins.__import__

        def tracked_import(name, *args, **kwargs):
            if name in ("communityai_desktop.profiles", "launch_node"):
                events.append(name)
                if name == "launch_node":
                    self.assertEqual(os.environ["HF_HOME"], str(self.profile.root / "cache" / "huggingface"))
                    self.assertNotIn("HF_TOKEN", os.environ)
            return real_import(name, *args, **kwargs)

        os.environ["HF_TOKEN"] = "fixture"
        with patch.object(
            launcher.multiprocessing, "freeze_support", side_effect=lambda: events.append("freeze")
        ), patch.object(builtins, "__import__", side_effect=tracked_import):
            self._run([])
        self.assertEqual(events[:3], ["freeze", "communityai_desktop.profiles", "launch_node"])

    def test_bootstrap_accepts_explicit_trust_input_but_only_fixed_write_targets(self):
        source = self.home / "read-only-bundle" / "catalog-bootstrap.json"
        source.parent.mkdir()
        source.write_bytes(b"trusted input fixture")
        args = [
            "bootstrap",
            str(source),
            "--data_dir",
            str(self.profile.data_dir),
            "--node_config",
            str(self.profile.config_path),
            "--refresh_if_needed",
        ]
        self.assertEqual(self._run(args), 0)
        dispatched, directory, environment = self.dispatches.pop()
        self.assertEqual(dispatched[1:], args[1:])
        self.assertEqual(directory, self.profile.data_dir)
        self.assertNotIn(launcher.PARENT_PID_ENV, environment)
        self.assertEqual(source.read_bytes(), b"trusted input fixture")
        for extra in (
            ["--data_dir", str(self.home / "ordinary")],
            ["--node_config", str(source)],
            ["--data", str(self.profile.data_dir)],
            ["--refresh"],
            ["--token", "secret"],
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self._run(["bootstrap", str(source), *extra])
        self.assertEqual(self.dispatches, [])

    def test_actual_gui_lifecycle_node_and_bootstrap_commands_satisfy_the_wrapper_contract(self):
        from communityai_desktop.lifecycle import NodeLifecycleSupervisor

        executable = self.home / "volunteer-node"
        executable.write_bytes(b"fixture executable")
        bootstrap_input = self.home / "catalog-bootstrap.json"
        bootstrap_input.write_bytes(b"fixture trust input")
        bootstrap_calls = []

        def bootstrap_runner(command, **kwargs):
            bootstrap_calls.append((command, kwargs))
            result = self._run(command[1:])
            self.profile.config_path.write_text('{"schema_version": 1, "models": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, result, stdout="{}", stderr="")

        supervisor = NodeLifecycleSupervisor(
            self.profile.node_url,
            types.SimpleNamespace(service=self.profile.credential_service, account=self.profile.credential_account),
            config_path=self.profile.config_path,
            data_dir=self.profile.data_dir,
            node_command=(str(executable),),
            bootstrap_config_path=bootstrap_input,
            bootstrap_runner=bootstrap_runner,
            pause_sharing_on_start=True,
            local_inference_cpu_only=True,
            allow_external_node=False,
        )
        self.assertEqual(self._run(supervisor._command()[1:]), 0)
        supervisor._ensure_config()
        supervisor._ensure_config()
        self.assertNotIn("--refresh_if_needed", bootstrap_calls[0][0])
        self.assertIn("--refresh_if_needed", bootstrap_calls[1][0])
        self.assertEqual(bootstrap_calls[0][0][1:3], ("bootstrap", str(bootstrap_input)))
        self.assertEqual(bootstrap_calls[0][1]["cwd"], str(self.profile.data_dir))
        self.assertEqual(len(self.dispatches), 3)

    def test_worker_requires_inherited_current_parent_and_profile_before_dispatch(self):
        arguments = self._worker()
        for parent, root in (
            (None, str(self.profile.root)),
            (str(os.getpid()), str(self.profile.root)),
            (str(os.getppid()), str(self.home / "ordinary")),
        ):
            with self.subTest(parent=parent, root=root):
                if parent is None:
                    os.environ.pop(launcher.PARENT_PID_ENV, None)
                else:
                    os.environ[launcher.PARENT_PID_ENV] = parent
                os.environ[launcher.PROFILE_ROOT_ENV] = root
                with self.assertRaisesRegex(ValueError, "supervisor parent"):
                    self._run(arguments)
                self.assertEqual(os.environ.get(launcher.PARENT_PID_ENV), parent)
        self.assertEqual(self.dispatches, [])

    def test_supervised_server_preserves_required_binding_options_and_private_paths(self):
        arguments = self._worker()
        cache = self.profile.root / "cache" / "drift" / "model"
        revocation = self.profile.data_dir / "revocation.json"
        revocation.write_text("{}", encoding="utf-8")
        arguments.extend(
            [
                "--cache_dir",
                str(cache),
                "--expected_cache_root",
                str(cache),
                "--expected_manifest_digest",
                "sha256:" + "a" * 64,
                "--expected_block_indices",
                "0:2",
                "--expected_artifact_bytes",
                "1024",
                "--expected_artifact_set_digest",
                "sha256:" + "b" * 64,
                "--max_disk_space",
                "5GiB",
                "--max_device_memory",
                "2000000000",
                "--processing_budget_path",
                str(self.profile.data_dir / ".processing-budget"),
                "--port",
                "31337",
                "--announce_maddrs",
                "/ip4/127.0.0.1/tcp/31337",
                "--revocation_file",
                str(revocation),
            ]
        )
        progress = self.profile.root / "tmp" / "worker" / "progress.json"
        os.environ["DRIFT_DOWNLOAD_PROGRESS"] = str(progress)
        os.environ["CUDA_VISIBLE_DEVICES"] = "GPU-fixture"
        self.assertEqual(self._run(arguments), 0)
        dispatched, directory, environment = self.dispatches[0]
        self.assertEqual(dispatched[1:], arguments[1:])
        self.assertEqual(directory, self.profile.data_dir)
        self.assertEqual(environment["DRIFT_DOWNLOAD_PROGRESS"], str(progress))
        self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "GPU-fixture")
        self.assertEqual(environment[launcher.PARENT_PID_ENV], str(os.getppid()))

    def test_server_rejects_custom_config_code_tokens_unknown_or_uncontained_paths(self):
        arguments = self._worker()
        for option, value in (
            ("--config", "config.yml"),
            ("--custom_module_path", "evil.py"),
            ("--token", "secret"),
            ("--use_auth_token", None),
            ("--allow_training_rpcs", None),
            ("--model_man", str(self.manifest)),
            ("--cache_dir", str(self.home / "ordinary")),
            ("--processing_budget_path", str(self.home / "ordinary" / "budget")),
            ("--revocation_file", str(self.home / "outside.json")),
            ("--identity_path", str(self.profile.data_dir / "other.key")),
        ):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self._run([*arguments, option, *((value,) if value is not None else ())])
        self.assertEqual(self.dispatches, [])

    def test_forwarded_option_values_cannot_be_reinterpreted_as_options(self):
        arguments = self._worker()
        # Even when the downstream parser would reject a missing value, the
        # wrapper must not turn an inline value into a new option boundary.
        for option, injected in (
            ("--initial_peers", "--config"),
            ("--throughput", "--token"),
            ("--announce_maddrs", "--custom_module_path"),
        ):
            with self.subTest(option=option):
                altered = list(arguments)
                if option in altered:
                    index = altered.index(option)
                    del altered[index : index + 2]
                altered.append(f"{option}={injected}")
                with self.assertRaisesRegex(ValueError, "invalid volunteer option value"):
                    self._run(altered)
        self.assertEqual(self.dispatches, [])

    def test_worker_accepts_equal_scalar_values_and_repeated_revocations_without_ambiguity(self):
        arguments = self._worker()
        index = arguments.index("--throughput")
        arguments[index : index + 2] = ["--throughput=1.0"]
        revocations = [self.profile.data_dir / f"revocation-{index}.json" for index in range(2)]
        for path in revocations:
            path.write_text("{}", encoding="utf-8")
            arguments.extend(("--revocation_file", str(path)))
        self._run(arguments)
        forwarded = self.dispatches[0][0]
        self.assertEqual(forwarded[forwarded.index("--throughput") + 1], "1.0")
        self.assertEqual(
            [forwarded[index + 1] for index, value in enumerate(forwarded) if value == "--revocation_file"],
            [str(path) for path in revocations],
        )

    def test_invalid_saved_profile_is_rejected_before_runtime_dispatch(self):
        self.profile.data_dir.mkdir(parents=True)
        self.profile.config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "models": [{"manifest": str(self.home / "ordinary.json")}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            self._run([])
        self.assertEqual(self.dispatches, [])

    def test_server_rejects_implicit_config_and_external_progress_output(self):
        arguments = self._worker()
        implicit = self.profile.data_dir / "config.yml"
        implicit.write_text("custom_module_path: ordinary.py\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "implicit config.yml"):
            self._run(arguments)
        self.assertIn("custom_module_path", implicit.read_text(encoding="utf-8"))
        implicit.unlink()
        os.environ["DRIFT_DOWNLOAD_PROGRESS"] = str(self.home / "ordinary" / "progress.json")
        with self.assertRaisesRegex(ValueError, "fixed profile root"):
            self._run(arguments)
        self.assertEqual(self.dispatches, [])
        self.assertFalse((self.home / "ordinary").exists())

    def test_supervised_edge_acquisition_is_private_and_always_disables_tokens(self):
        self._worker()
        cache = self.profile.root / "cache" / "edge"
        output = self.profile.data_dir / "edge-result.json"
        self.assertEqual(
            self._run(
                [
                    "edge-acquire",
                    str(self.manifest),
                    "--cache_dir",
                    str(cache),
                    "--output",
                    str(output),
                    "--max_resumptions",
                    "1",
                ]
            ),
            0,
        )
        dispatched = self.dispatches.pop()[0]
        self.assertIn("--no_token", dispatched)
        self.assertEqual(dispatched[dispatched.index("--cache_dir") + 1], str(cache))
        self.assertEqual(
            self._run(
                [
                    "edge-acquire",
                    "--manifest_stdin_sha256",
                    "sha256:" + "a" * 64,
                    "--cache_dir",
                    str(cache),
                    "--require_direct_upstream",
                ]
            ),
            0,
        )
        self.dispatches.clear()
        for extra in (
            ["--token", "secret"],
            ["--output", str(self.home / "ordinary.json")],
            ["--cache", str(cache)],
            ["--manifest_stdin_sha256", "sha256:" + "a" * 64],
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self._run(["edge-acquire", str(self.manifest), "--cache_dir", str(cache), *extra])
        os.environ.pop(launcher.PARENT_PID_ENV, None)
        with self.assertRaisesRegex(ValueError, "supervisor parent"):
            self._run(["edge-acquire", str(self.manifest), "--cache_dir", str(cache)])
        self.assertEqual(self.dispatches, [])

    def test_linked_and_hardlinked_worker_inputs_are_rejected(self):
        arguments = self._worker()
        outside = self.home / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        linked = self.profile.data_dir / "linked.json"
        os.link(outside, linked)
        altered = list(arguments)
        altered[altered.index("--model_manifest") + 1] = str(linked)
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            self._run(altered)
        linked.unlink()
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink fixtures unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self._run(altered)
        self.assertEqual(outside.read_text(encoding="utf-8"), "{}")
        self.assertEqual(self.dispatches, [])

    def test_exact_diagnostics_remain_available_without_supervisor_and_in_isolated_state(self):
        os.environ.pop(launcher.PARENT_PID_ENV, None)
        cases = (
            ["--self-test"],
            ["server", "--self-test"],
            ["--native-self-test"],
            ["--native-self-test", "--require-cuda"],
            ["--cgroup-extension-self-test"],
            ["--help"],
            ["server", "--help"],
            ["bootstrap", "--help"],
            ["edge-acquire", "--help"],
        )
        fake = types.ModuleType("launch_node")
        fake.main = lambda: self.dispatches.append((list(sys.argv), Path.cwd(), os.environ.copy())) or 0
        with patch.dict(sys.modules, {"launch_node": fake}):
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    self.assertEqual(launcher.main(arguments), 0)
                    dispatched, directory, environment = self.dispatches.pop()
                    self.assertEqual(dispatched[1:], arguments)
                    self.assertEqual(directory, self.profile.data_dir)
                    self.assertNotIn(launcher.PARENT_PID_ENV, environment)
                    self.assertTrue(Path(environment["HF_HOME"]).is_relative_to(self.profile.root))
        for arguments in (
            ["server", "--self-test", "--config", "ordinary.yml"],
            ["--self-test", "--data_dir", str(self.home)],
            ["--native-self-test", "unexpected"],
            ["--cgroup-extension-self-test", "--worker-cgroup-root", "/private/root"],
            ["--cgroup-extension-self-test=/private/root"],
            ["server", "--cgroup-extension-self-test"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self._run(arguments)

    def test_real_parent_child_inheritance_accepts_only_the_node_marked_parent(self):
        arguments = self._worker()
        fixture = self.home / "dispatch_fixture.py"
        fixture.write_text(
            "import json, os, subprocess, sys, types\n"
            "import launch_volunteer_node as wrapper\n"
            "runtime = types.ModuleType('launch_node')\n"
            "worker = json.loads(os.environ['TEST_WORKER_ARGS'])\n"
            "def dispatch():\n"
            "    if sys.argv[1] == 'server':\n"
            "        print(json.dumps({'parent': os.getppid(), 'marker': os.environ[wrapper.PARENT_PID_ENV],"
            " 'cwd': os.getcwd()}))\n"
            "        return 0\n"
            "    env = os.environ.copy()\n"
            "    ok = subprocess.run([sys.executable, __file__, 'child'], env=env, capture_output=True, text=True)\n"
            "    env[wrapper.PARENT_PID_ENV] = '0'\n"
            "    bad = subprocess.run([sys.executable, __file__, 'child'], env=env, capture_output=True, text=True)\n"
            "    print(json.dumps({'pid': os.getpid(), 'ok': ok.returncode, 'child': ok.stdout,"
            " 'child_error': ok.stderr, 'bad': bad.returncode, 'error': bad.stderr}))\n"
            "    return 0\n"
            "runtime.main = dispatch\n"
            "sys.modules['launch_node'] = runtime\n"
            "# This fixture isolates parent-marker inheritance, not Linux anchor authority.\n"
            "entry = types.ModuleType('drift.node.linux_anchor_entry')\n"
            "entry.NODE_TOKEN_ENV = 'COMMUNITYAI_ANCHOR_NODE_TOKEN'\n"
            "entry.validate_node_entry = lambda *args: None\n"
            "sys.modules['drift.node.linux_anchor_entry'] = entry\n"
            "raise SystemExit(wrapper.main(worker if sys.argv[1:] == ['child'] else []))\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["TEST_WORKER_ARGS"] = json.dumps(arguments)
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(DESKTOP), str(DESKTOP / "src"), environment.get("PYTHONPATH", ""))
        )
        # Windows virtualenv redirectors insert an extra launcher parent. The
        # base interpreter gives this stdlib-only fixture a real direct parent,
        # as the supported Linux frozen executable has; do not weaken PID checks.
        fixture_python = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
        result = subprocess.run(
            [fixture_python, str(fixture)],
            env=environment,
            cwd=self.home,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["ok"], 0, evidence["child_error"])
        child = json.loads(evidence["child"])
        self.assertEqual(child["parent"], evidence["pid"])
        self.assertEqual(child["marker"], str(evidence["pid"]))
        self.assertEqual(child["cwd"], str(self.profile.data_dir))
        self.assertNotEqual(evidence["bad"], 0)
        self.assertIn("supervisor parent", evidence["error"])


if __name__ == "__main__":
    unittest.main()
