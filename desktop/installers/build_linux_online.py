"""Generate a standalone Python 3.9+ Linux installer pinned to one release manifest entry."""

import argparse
import hashlib
import json
from pathlib import Path

from release_downloads import load_release_manifest, select_artifact


def build(manifest_path, output):
    output = Path(output)
    metadata_path = output.with_suffix(output.suffix + ".json")
    if output.exists() or output.is_symlink() or metadata_path.exists() or metadata_path.is_symlink():
        raise FileExistsError("Online installer output or metadata already exists")
    artifact = select_artifact(load_release_manifest(manifest_path), "linux-amd64")
    template = Path(__file__).with_name("linux_online_template.py").read_text(encoding="utf-8")
    marker = "ARTIFACT = None  # Replaced with a validated release entry by build_linux_online.py."
    if template.count(marker) != 1:
        raise ValueError("Linux online installer template marker is missing or ambiguous")
    # Python repr produces a literal, never executable text from a manifest URL.
    rendered = template.replace(marker, "ARTIFACT = " + repr(artifact))
    helper_marker = "ROOT_HELPER = None  # Replaced with the isolated protected-copy helper by the builder."
    if rendered.count(helper_marker) != 1:
        raise ValueError("Linux protected installation helper marker is missing or ambiguous")
    helper = Path(__file__).with_name("linux_online_root.py").read_text(encoding="utf-8")
    rendered = rendered.replace(helper_marker, "ROOT_HELPER = " + repr(helper))
    compile(rendered, str(output), "exec")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)
    output.chmod(0o755)
    encoded = rendered.encode("utf-8")
    metadata = {
        "schema_version": 1,
        "kind": "online-installer",
        "platform": "linux-amd64",
        "format": "python3-script",
        "minimum_python": "3.9",
        "version": artifact["version"],
        "filename": output.name,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
        "offline_installer": artifact,
        "publisher_signature": False,
        "live_download_verified": False,
    }
    with metadata_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps({"artifact": str(output), "package_url": artifact["url"], "package_sha256": artifact["sha256"]}))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="new output .py file; existing files are never replaced"
    )
    args = parser.parse_args()
    build(args.manifest, args.output)


if __name__ == "__main__":
    main()
