"""Verify the emergency Google Secret Manager backup without printing private material."""

import argparse
import json
import shutil
import subprocess

from cryptography.hazmat.primitives import serialization

from drift.model_catalog import CatalogSigningKey

PROJECT = "community-ai-506321"
SECRET = "communityai-catalog-signer-20260906"
VERSION = "1"
KEY_ID = "sha256:9505d3ac8ec996d4b794bd43d09dd84447acc5da8a0bb1eac1be4e9c9a34b14f"


def load_online_backup(project=PROJECT, secret=SECRET, version=VERSION, expected_key_id=KEY_ID):
    if not version.isdigit() or int(version) < 1:
        raise ValueError("Recovery must pin a numeric secret version")
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        raise RuntimeError("Google Cloud CLI is required for emergency backup verification")
    response = subprocess.run(
        [gcloud, "secrets", "versions", "access", version, "--secret", secret, "--project", project, "--quiet"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if response.returncode:
        # Neither stdout nor stderr is included in an exception or a test report.
        raise RuntimeError("Could not retrieve the pinned emergency backup with the current Google identity")
    recovered = CatalogSigningKey(serialization.load_pem_private_key(response.stdout, password=None))
    if recovered.key_id != expected_key_id:
        raise ValueError("Emergency backup public identity does not match the release trust root")
    return recovered


def verify_backup():
    key = load_online_backup()
    challenge = b"CommunityAI emergency publisher backup recovery drill v1"
    key.trusted_key.public_key_object.verify(key.sign(challenge), challenge)
    return {
        "result": "passed",
        "project": PROJECT,
        "secret": SECRET,
        "version": VERSION,
        "key_id": key.key_id,
        "recovered_in_memory": True,
        "signature_verified": True,
        "private_material_printed": False,
    }


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(verify_backup(), sort_keys=True))
