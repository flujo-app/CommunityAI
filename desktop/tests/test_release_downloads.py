import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from installers import release_downloads


def artifact(platform="windows-x64"):
    windows = platform == "windows-x64"
    version = "0.1.0-alpha.1" if windows else "0.1.0~alpha.1"
    filename = f"communityai-{version}-windows-setup.exe" if windows else f"communityai_{version}_amd64.deb"
    return {
        "platform": platform,
        "kind": "offline-installer",
        "format": "exe" if windows else "deb",
        "version": version,
        "filename": filename,
        "url": f"https://downloads.example.invalid/alpha/{filename}",
        "sha256": hashlib.sha256(b"fixture offline installer").hexdigest(),
        "size_bytes": len(b"fixture offline installer"),
        "publisher": "CommunityAI engineering",
    }


class ReleaseDownloadsTests(unittest.TestCase):
    def test_one_or_both_pinned_platforms_and_sizes_above_two_gib(self):
        entries = {platform: artifact(platform) for platform in release_downloads.PLATFORMS}
        entries["windows-x64"]["size_bytes"] = 2_519_046_440
        entries["linux-amd64"]["size_bytes"] = 3_781_591_484
        manifest = {"schema_version": 1, "artifacts": entries}
        self.assertEqual(release_downloads.select_artifact(manifest, "linux-amd64"), entries["linux-amd64"])
        one = {"schema_version": 1, "artifacts": {"windows-x64": entries["windows-x64"]}}
        self.assertEqual(release_downloads.validate_release_manifest(one), one)

    def test_invalid_pins_and_metadata_are_rejected(self):
        mutations = (
            {"kind": "pip-wheel"},
            {"platform": "linux-amd64"},
            {"format": "zip"},
            {"filename": "../setup.exe"},
            {"filename": "other.exe"},
            {"version": "latest"},
            {"version": "0.1.0\nmalicious"},
            {"sha256": "0" * 63},
            {"sha256": "F" * 64},
            {"size_bytes": True},
            {"size_bytes": 0},
            {"size_bytes": 4.0},
            {"size_bytes": release_downloads.MAX_PACKAGE_BYTES + 1},
            {"publisher": 'bad"publisher'},
            {"publisher": "bad\npublisher"},
            {"command": "arbitrary command"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                release_downloads.validate_artifact(dict(artifact(), **mutation), "windows-x64")

    def test_network_and_path_confusion_is_rejected(self):
        good = artifact()
        filename = good["filename"]
        urls = (
            f"http://downloads.example.invalid/{filename}",
            f"https://user:secret@downloads.example.invalid/{filename}",
            f"https://downloads.example.invalid:8443/{filename}",
            good["url"] + "?token=secret",
            good["url"] + "#fragment",
            f"https://downloads.example.invalid/../{filename}",
            f"https://downloads.example.invalid/%2e%2e/{filename}",
            f"https://downloads.example.invalid/%2f/{filename}",
            f"https://downloads.example.invalid/%5c/{filename}",
            f"https://downloads.example.invalid/%00/{filename}",
            f"https://downloads.example.invalid/%ZZ/{filename}",
            "https://downloads.example.invalid/different.exe",
            "https://downloads.example.invalid/\n" + filename,
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                release_downloads.validate_artifact(dict(good, url=url))

    def test_manifest_shape_cannot_silently_select_wrong_platform(self):
        for manifest in (
            {"schema_version": True, "artifacts": {"windows-x64": artifact()}},
            {"schema_version": 2, "artifacts": {"windows-x64": artifact()}},
            {"schema_version": 1, "artifacts": {}},
            {"schema_version": 1, "artifacts": {"linux-amd64": artifact()}},
            {"schema_version": 1, "artifacts": {"macos": artifact()}},
            {"schema_version": 1, "artifacts": {"windows-x64": artifact()}, "latest": True},
        ):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                release_downloads.validate_release_manifest(manifest)
        with self.assertRaises(ValueError):
            release_downloads.select_artifact(
                {"schema_version": 1, "artifacts": {"windows-x64": artifact()}}, "linux-amd64"
            )

    def test_file_hash_and_size_are_derived_from_the_exact_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = artifact("linux-amd64")
            payload = Path(directory) / expected["filename"]
            payload.write_bytes(b"fixture offline installer")
            result = release_downloads.artifact_from_file(
                "linux-amd64", payload, expected["version"], "https://downloads.example.invalid/alpha"
            )
            expected.pop("publisher")
            self.assertEqual(result, expected)
            payload.write_bytes(b"different package")
            updated = release_downloads.artifact_from_file(
                "linux-amd64", payload, expected["version"], "https://downloads.example.invalid/alpha"
            )
            self.assertNotEqual(updated["sha256"], result["sha256"])
            self.assertEqual(updated["size_bytes"], len(b"different package"))

    def test_duplicate_json_fields_and_oversized_manifests_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.json"
            raw = json.dumps({"schema_version": 1, "artifacts": {"windows-x64": artifact()}})
            path.write_text(raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
            with self.assertRaises(ValueError):
                release_downloads.load_release_manifest(path)
            path.write_text(" " * (release_downloads.MAX_MANIFEST_BYTES + 1))
            with self.assertRaises(ValueError):
                release_downloads.load_release_manifest(path)


if __name__ == "__main__":
    unittest.main()
