from __future__ import annotations

import builtins
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

DESKTOP = Path(__file__).resolve().parents[1]


def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "anchor_credential_helper_launcher_test",
        DESKTOP / "launch_volunteer_node.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


class AnchorCredentialHelperLauncherTests(unittest.TestCase):
    @staticmethod
    def _guard_profile_import(events):
        original_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            events.append(("import", name))
            if name == "communityai_desktop.profiles":
                raise AssertionError("credential helper dispatch must precede profile imports")
            return original_import(name, *args, **kwargs)

        return guarded

    def test_exact_frozen_linux_helper_dispatches_after_freeze_support_without_profile_access(self):
        events = []
        helper = types.ModuleType("communityai_anchor.linux_anchor_credentials")

        def helper_main():
            events.append(("helper", ()))
            return 23

        helper.credential_helper_main = helper_main
        before_environment = os.environ.copy()
        before_directory = Path.cwd()
        with (
            patch.dict(sys.modules, {helper.__name__: helper}),
            patch.object(sys, "platform", "linux"),
            patch.object(sys, "frozen", True, create=True),
            patch.object(launcher.multiprocessing, "freeze_support", side_effect=lambda: events.append(("freeze", ()))),
            patch.object(builtins, "__import__", side_effect=self._guard_profile_import(events)),
        ):
            self.assertEqual(launcher.main(["--anchor-credential-helper"]), 23)

        self.assertEqual([event[0] for event in events if event[0] != "import"], ["freeze", "helper"])
        self.assertFalse(any(event == ("import", "communityai_desktop.profiles") for event in events))
        self.assertEqual(dict(os.environ), before_environment)
        self.assertEqual(Path.cwd(), before_directory)

    def test_helper_refuses_extras_non_linux_and_non_frozen_before_profile_or_backend(self):
        cases = (
            ("linux", True, ["--anchor-credential-helper", "--reset"]),
            ("win32", True, ["--anchor-credential-helper"]),
            ("linux", False, ["--anchor-credential-helper"]),
        )
        for platform_name, frozen, arguments in cases:
            with self.subTest(platform=platform_name, frozen=frozen, arguments=arguments):
                events = []
                helper = types.ModuleType("communityai_anchor.linux_anchor_credentials")

                def helper_main():
                    raise AssertionError("rejected helper invocation must not call the credential backend")

                helper.credential_helper_main = helper_main
                before_environment = os.environ.copy()
                before_directory = Path.cwd()
                with (
                    patch.dict(sys.modules, {helper.__name__: helper}),
                    patch.object(sys, "platform", platform_name),
                    patch.object(sys, "frozen", frozen, create=True),
                    patch.object(
                        launcher.multiprocessing,
                        "freeze_support",
                        side_effect=lambda: events.append(("freeze", ())),
                    ) as freeze,
                    patch.object(builtins, "__import__", side_effect=self._guard_profile_import(events)),
                ):
                    self.assertEqual(launcher.main(arguments), 75)

                freeze.assert_called_once_with()
                self.assertFalse(any(event == ("import", "communityai_desktop.profiles") for event in events))
                self.assertEqual(dict(os.environ), before_environment)
                self.assertEqual(Path.cwd(), before_directory)

    def test_anchor_factory_passes_identity_only_and_fixed_executor_without_parent_keyring_access(self):
        from communityai_desktop.profiles import VolunteerProfile

        from communityai_anchor import linux_anchor_credentials as credentials

        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            executable = home / "CommunityAI-Node"
            executable.write_bytes(b"frozen-sidecar-fixture")
            profile = VolunteerProfile(home / ".communityai" / "multigpu-volunteer")
            plan = object()
            preparation = object()
            executor = object()
            layout = types.SimpleNamespace(validate=Mock())
            original_identity = credentials.CredentialIdentity
            original_import = builtins.__import__
            drift = types.ModuleType("drift")
            drift.__path__ = []
            drift_node = types.ModuleType("drift.node")
            drift_node.__path__ = []
            anchor = types.ModuleType("drift.node.linux_anchor")
            anchor._require = lambda condition: condition or (_ for _ in ()).throw(ValueError("fixture refusal"))
            bootstrap_module = types.ModuleType("drift.node.linux_anchor_bootstrap")
            bootstrap_module.AnchorBootstrap = Mock(return_value=preparation)
            node_module = types.ModuleType("drift.node.linux_anchor_node")
            node_module.AnchorNode = Mock(return_value="controller")
            state_module = types.ModuleType("drift.node.linux_anchor_state")
            state_module._sync_directory = Mock()
            runtime_modules = {
                "drift": drift,
                "drift.node": drift_node,
                anchor.__name__: anchor,
                bootstrap_module.__name__: bootstrap_module,
                node_module.__name__: node_module,
                state_module.__name__: state_module,
            }

            def forbid_parent_store(name, *args, **kwargs):
                if name == "communityai_desktop.credentials":
                    raise AssertionError("anchor parent must not import the synchronous credential store")
                return original_import(name, *args, **kwargs)

            with (
                patch.dict(sys.modules, runtime_modules),
                patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
                patch.object(sys, "platform", "linux"),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(launcher, "_packaged_bootstrap_plan", return_value=plan),
                patch.object(credentials.CredentialExecutor, "for_profile", return_value=executor) as make_executor,
                patch.object(credentials, "CredentialIdentity", wraps=original_identity) as make_identity,
                patch.object(builtins, "__import__", side_effect=forbid_parent_store),
            ):
                self.assertEqual(launcher._anchor_controller(layout), "controller")

            layout.validate.assert_called_once_with()
            make_identity.assert_called_once_with(profile.credential_service, profile.credential_account)
            bootstrap = bootstrap_module.AnchorBootstrap
            owner = node_module.AnchorNode
            identity = bootstrap.call_args.args[2]
            self.assertIsInstance(identity, original_identity)
            self.assertFalse(hasattr(identity, "get"))
            self.assertFalse(hasattr(identity, "set"))
            make_executor.assert_called_once_with(profile)
            self.assertEqual(bootstrap.call_args.args[:2], (plan, profile))
            self.assertIs(owner.call_args.kwargs["bootstrap"], preparation)
            self.assertIs(owner.call_args.kwargs["credential_executor"], executor)


if __name__ == "__main__":
    unittest.main()
