"""Sign a complete release manifest for the desktop updater; key supplied on stdin."""

import argparse
import base64
import json
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from release_downloads import load_release_manifest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from communityai_desktop.updater import PUBLIC_KEY, SIGNATURE_DOMAIN, canonical, verify_feed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = load_release_manifest(args.manifest)
    artifacts = {}
    for target, item in manifest["artifacts"].items():
        expected = args.version.replace("-alpha.", "~alpha.") if target == "linux-amd64" else args.version
        if item["version"] != expected:
            raise ValueError("Every installer must match the update release")
        artifacts[target] = {key: item[key] for key in ("filename", "url", "size_bytes", "sha256")}
    now = int(time.time())
    signed = {
        "schema_version": 1,
        "channel": "alpha",
        "version": args.version,
        "sequence": args.sequence,
        "published_at": now,
        "expires_at": now + 90 * 86400,
        "artifacts": artifacts,
    }
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(sys.stdin.read().strip(), validate=True))
    document = {
        "signed": signed,
        "signature": base64.b64encode(key.sign(SIGNATURE_DOMAIN + canonical(signed))).decode(),
    }
    raw = canonical(document)
    verify_feed(raw, public_key=PUBLIC_KEY)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw + b"\n")
    print(json.dumps({"signed_update": str(args.output), "version": args.version, "sequence": args.sequence}))


if __name__ == "__main__":
    main()
