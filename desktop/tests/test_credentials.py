from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from communityai_desktop.app import build_parser, main
from communityai_desktop.credentials import CredentialError, NativeCredentialStore

CONTROL_KEY = "drift_control_" + "B" * 43


class FakeKeyringError(Exception):
    pass


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, secret):
        self.values[(service, account)] = secret

    def delete_password(self, service, account):
        del self.values[(service, account)]


class NativeCredentialStoreTests(unittest.TestCase):
    def test_round_trips_a_valid_control_key(self):
        backend = FakeKeyring()
        store = NativeCredentialStore()
        with patch.object(store, "_keyring", return_value=(backend, FakeKeyringError)):
            store.set(CONTROL_KEY)
            self.assertEqual(store.get(), CONTROL_KEY)
            self.assertTrue(store.delete())
            self.assertFalse(store.delete())

    def test_rejects_an_inference_key_or_empty_value(self):
        backend = FakeKeyring()
        store = NativeCredentialStore()
        with patch.object(store, "_keyring", return_value=(backend, FakeKeyringError)):
            for secret in ("", "drift_client-key", "drift_control_short"):
                with self.subTest(secret=secret), self.assertRaises(CredentialError):
                    store.set(secret)

    def test_product_cli_has_no_private_file_or_secret_argument(self):
        help_text = build_parser().format_help()
        self.assertNotIn("control-key-file", help_text)
        self.assertNotIn("control-token", help_text)

    def test_rejects_non_loopback_url_before_opening_keyring(self):
        with patch("communityai_desktop.app.NativeCredentialStore.get_or_migrate") as get:
            with self.assertRaises(SystemExit) as caught:
                main(["--node-url", "https://example.com", "--probe-only"])
        self.assertEqual(caught.exception.code, 2)
        get.assert_not_called()

    def test_normal_startup_opens_the_window_before_loading_a_credential(self):
        with patch("communityai_desktop.app.NativeCredentialStore.get_or_migrate") as get, patch(
            "communityai_desktop.pyside_shell.run", return_value=0
        ) as run:
            self.assertEqual(main([]), 0)

        get.assert_not_called()
        run.assert_called_once()
        connector = run.call_args.kwargs["connect"]
        with patch(
            "communityai_desktop.app.NativeCredentialStore.get_or_migrate",
            side_effect=CredentialError("missing"),
        ):
            with self.assertRaisesRegex(CredentialError, "missing"):
                connector()

    def test_imports_an_existing_headless_key_without_a_setup_step(self):
        backend = FakeKeyring()
        store = NativeCredentialStore()
        with TemporaryDirectory() as directory:
            key_path = Path(directory) / "control-api.key"
            key_path.write_text(CONTROL_KEY + "\n", encoding="utf-8")
            key_path.chmod(0o600)
            with patch.object(store, "_keyring", return_value=(backend, FakeKeyringError)):
                self.assertEqual(store.get_or_migrate(key_path), CONTROL_KEY)
                self.assertEqual(store.get(), CONTROL_KEY)


if __name__ == "__main__":
    unittest.main()
