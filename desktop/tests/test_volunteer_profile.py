from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from communityai_desktop.app import main, volunteer_main
from communityai_desktop.credentials import DEFAULT_CREDENTIAL_ACCOUNT, DEFAULT_CREDENTIAL_SERVICE, CredentialProvision
from communityai_desktop.lifecycle import DEFAULT_NODE_DATA_DIR, NodeLifecycleError, NodeLifecycleSupervisor
from communityai_desktop.profiles import VolunteerProfile


class VolunteerProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.profile = VolunteerProfile(self.base / "test-profile")

    def write_config(self, **changes):
        self.profile.data_dir.mkdir(parents=True, exist_ok=True)
        document = {"schema_version": 1, "models": [{"manifest": "manifests/model.json", "initial_peers": []}]}
        document.update(changes)
        self.profile.config_path.write_text(json.dumps(document), encoding="utf-8")

    def test_profile_identities_are_distinct_and_paths_are_private(self):
        self.profile.prepare()
        self.assertNotEqual(self.profile.data_dir, DEFAULT_NODE_DATA_DIR)
        self.assertNotEqual(self.profile.credential_service, DEFAULT_CREDENTIAL_SERVICE)
        self.assertNotEqual(self.profile.credential_account, DEFAULT_CREDENTIAL_ACCOUNT)
        self.assertNotEqual(self.profile.node_url, "http://127.0.0.1:8080")
        self.assertTrue(self.profile.instance_dir.is_dir())
        if os.name != "nt":
            for path in (self.profile.root, self.profile.data_dir, self.profile.instance_dir):
                self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_config_external_paths_rejected_without_touching_regular_files(self):
        outside = self.base / "regular-app"
        outside.mkdir()
        original = outside / "identity.key"
        original.write_bytes(b"regular app state")
        cases = [
            {"models": [{"manifest": str(original)}]},
            {"models": [{"manifest": "model.json", "cache_dir": str(outside)}]},
            {"models": [{"manifest": "model.json", "revocation_files": [str(original)]}]},
            {"workers": [{"identity_path": str(original)}]},
            {"workers": [{"identity_path": "worker.key", "cache_dir": str(outside)}]},
            {"catalog_path": str(original)},
            {"catalog_bootstrap_path": str(original)},
            {"models": [{"manifest": "../../regular-app/identity.key"}]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.write_config(**changes)
                before = self.profile.config_path.read_bytes()
                with self.assertRaisesRegex(ValueError, "outside"):
                    self.profile.validate_config()
                self.assertEqual(original.read_bytes(), b"regular app state")
                self.assertEqual(self.profile.config_path.read_bytes(), before)

    def test_configuration_links_into_regular_profile_are_rejected(self):
        outside = self.base / "regular-app"
        outside.mkdir()
        self.profile.root.mkdir()
        alias = self.profile.data_dir
        try:
            alias.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink fixture unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "links|redirected|junctions"):
            self.profile.prepare()
        self.assertEqual(list(outside.iterdir()), [])

    def test_configuration_hardlink_is_rejected(self):
        self.write_config()
        shared = self.base / "regular-config.json"
        os.link(self.profile.config_path, shared)
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            self.profile.validate_config()

    def test_shared_api_key_hardlink_is_rejected_before_startup(self):
        self.profile.data_dir.mkdir(parents=True)
        original = self.base / "regular-api.key"
        original.write_text("regular-app-secret", encoding="utf-8")
        os.link(original, self.profile.data_dir / "local-api.key")
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            self.profile.prepare()
        self.assertEqual(original.read_text(), "regular-app-secret")

    def test_duplicate_and_oversized_configs_fail_closed(self):
        self.write_config()
        for source in ('{"models":[],"models":[]}', " " * (256 * 1024 + 1)):
            self.profile.config_path.write_text(source, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.profile.validate_config()

    def test_saved_local_preferences_are_not_rewritten_by_desktop(self):
        for device in ("auto", "cpu", "cuda:0"):
            self.write_config(models=[{"manifest": "model.json", "execution": "local", "local_device": device}])
            before = self.profile.config_path.read_bytes()
            self.profile.validate_config()
            self.assertEqual(self.profile.config_path.read_bytes(), before)

    def test_profile_cli_rejects_ordinary_paths_and_attach_before_keyring(self):
        cases = [
            ["--node-url=http://127.0.0.1:8080"],
            ["--node-config", "normal.json"],
            ["--node-data-dir", "normal"],
            ["--credential-service", "ordinary"],
            ["--credential-account", "ordinary"],
            ["--bootstrap-config", "ordinary.json"],
            ["--no-manage-node"],
            ["--started-at-login"],
            ["--node-u=http://127.0.0.1:8080"],
        ]
        with patch("communityai_desktop.app.NativeCredentialStore") as store:
            for options in cases:
                with self.subTest(options=options), self.assertRaises(SystemExit) as raised:
                    main(["--profile", "multigpu-volunteer", *options])
                self.assertEqual(raised.exception.code, 2)
            store.assert_not_called()

    def test_app_wires_fixed_profile_and_never_constructs_normal_updater(self):
        # Legacy direct supervisor wiring is the non-Linux path; Linux anchor
        # wiring and refused maintenance have separate platform tests.
        with patch("communityai_desktop.app.sys.platform", "win32"), patch(
            "communityai_desktop.app.VolunteerProfile.for_current_user", return_value=self.profile
        ), patch("communityai_desktop.app.NativeCredentialStore") as store, patch(
            "communityai_desktop.app.NodeLifecycleSupervisor"
        ) as lifecycle, patch(
            "communityai_desktop.pyside_shell.run", return_value=0
        ) as run, patch(
            "communityai_desktop.updater.installed_root"
        ) as installed:
            self.assertEqual(main(["--profile", "multigpu-volunteer"]), 0)
        store.assert_called_once_with(self.profile.credential_service, self.profile.credential_account)
        kwargs = lifecycle.call_args.kwargs
        self.assertEqual(kwargs["config_path"], self.profile.config_path)
        self.assertEqual(kwargs["data_dir"], self.profile.data_dir)
        self.assertTrue(kwargs["pause_sharing_on_start"])
        self.assertTrue(kwargs["local_inference_cpu_only"])
        self.assertFalse(kwargs["allow_external_node"])
        self.assertFalse(run.call_args.kwargs["allow_login_startup"])
        self.assertEqual(run.call_args.kwargs["instance_data_dir"], self.profile.instance_dir)
        self.assertIsNone(run.call_args.kwargs["updater"])
        installed.assert_not_called()
        lifecycle.return_value.close.assert_called_once()

    def test_dedicated_launcher_cannot_select_regular_profile(self):
        with patch("communityai_desktop.app.NativeCredentialStore") as store:
            for option in (["--profile=standard"], ["--profile", "multigpu-volunteer"]):
                with self.assertRaises(SystemExit) as raised:
                    volunteer_main(option)
                self.assertEqual(raised.exception.code, 2)
        store.assert_not_called()

    def test_profile_maintenance_still_works_when_config_is_invalid(self):
        self.write_config(workers=[{"identity_path": "../../outside.key"}])
        with patch("communityai_desktop.app.sys.platform", "win32"), patch(
            "communityai_desktop.app.VolunteerProfile.for_current_user", return_value=self.profile
        ), patch("communityai_desktop.maintenance.prepare_update", return_value=0) as stop, patch(
            "communityai_desktop.app.NativeCredentialStore"
        ) as store:
            self.assertEqual(main(["--profile", "multigpu-volunteer", "--prepare-update"]), 0)
        stop.assert_called_once_with(
            application_name=self.profile.application_name, instance_data_dir=self.profile.instance_dir
        )
        store.assert_not_called()

    def test_unmanaged_custom_data_never_migrates_regular_key(self):
        with patch("communityai_desktop.app.NativeCredentialStore") as store, patch(
            "communityai_desktop.app.DesktopController"
        ) as controller, patch("communityai_desktop.app._write_json"):
            store.return_value.get_or_migrate.return_value = "drift_control_" + "M" * 43
            controller.return_value.snapshot.return_value = {}
            self.assertEqual(main(["--no-manage-node", "--node-data-dir", str(self.base), "--probe-only"]), 0)
        store.return_value.get_or_migrate.assert_called_once_with(self.base / "control-api.key")

    def supervisor(self, **kwargs):
        store = Mock(service="profile-service", account="profile-account")
        store.provision.return_value = CredentialProvision("drift_control_" + "P" * 43, "native")
        return NodeLifecycleSupervisor(
            self.profile.node_url,
            store,
            config_path=self.profile.config_path,
            data_dir=self.profile.data_dir,
            node_command=("python", "node-fixture.py"),
            **kwargs,
        )

    def test_profile_does_not_adopt_an_existing_node(self):
        factory = Mock()
        client = Mock()
        supervisor = self.supervisor(
            allow_external_node=False, port_probe=lambda *args: True, process_factory=factory, client_factory=client
        )
        with self.assertRaisesRegex(NodeLifecycleError, "already in use"):
            supervisor.ensure_client()
        client.return_value.status.assert_not_called()
        factory.assert_not_called()

    def test_both_children_receive_private_cache_environment_without_parent_mutation(self):
        self.profile.prepare()
        bootstrap = self.base / "bootstrap.json"
        bootstrap.write_text("{}")
        calls = []

        def provision(command, **kwargs):
            calls.append((command, kwargs))
            self.write_config()
            return Mock(returncode=0)

        factory = Mock(return_value=Mock())
        supervisor = self.supervisor(
            bootstrap_config_path=bootstrap,
            bootstrap_command=("bootstrap-fixture",),
            bootstrap_runner=provision,
            process_factory=factory,
            environment_overrides=self.profile.child_environment(),
            validate_config=self.profile.validate_config,
            pause_sharing_on_start=True,
            local_inference_cpu_only=True,
        )
        inherited = {
            "DRIFT_CACHE": "ordinary",
            "HF_TOKEN_PATH": "ordinary-token",
            "HF_HUB_OFFLINE": "1",
            "HF_TOKEN": "test-token-sentinel",
            "HUGGING_FACE_HUB_TOKEN": "legacy-test-sentinel",
        }
        with patch.dict(os.environ, inherited):
            supervisor._start()
            for _, kwargs in (calls[0], factory.call_args):
                for name, path in self.profile.child_environment().items():
                    if path is None:
                        self.assertNotIn(name, kwargs["env"])
                    else:
                        self.assertEqual(kwargs["env"][name], path)
                self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["DRIFT_CACHE"], "ordinary")
            self.assertEqual(os.environ["HF_TOKEN_PATH"], "ordinary-token")
            self.assertEqual(os.environ["HF_TOKEN"], "test-token-sentinel")
        self.assertIn("--pause_sharing_on_start", factory.call_args.args[0])
        self.assertIn("--local_inference_cpu_only", factory.call_args.args[0])

    def test_unsafe_bootstrap_output_never_spawns_a_node(self):
        self.profile.prepare()
        bootstrap = self.base / "bootstrap.json"
        bootstrap.write_text("{}")

        def provision(*args, **kwargs):
            self.write_config(workers=[{"identity_path": str(self.base / "regular.key")}])
            return Mock(returncode=0)

        factory = Mock()
        supervisor = NodeLifecycleSupervisor(
            self.profile.node_url,
            Mock(service="profile", account="test"),
            config_path=self.profile.config_path,
            data_dir=self.profile.data_dir,
            node_command=("python", "node-fixture.py"),
            bootstrap_command=("python", "bootstrap-fixture.py"),
            bootstrap_config_path=bootstrap,
            bootstrap_runner=provision,
            process_factory=factory,
            validate_config=self.profile.validate_config,
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            supervisor._start()
        factory.assert_not_called()
