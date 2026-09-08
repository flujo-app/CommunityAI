"""Pinned offline-package metadata shared by the Windows and Linux online builders.

The online installers embed this metadata at build time. They never resolve a
mutable remote manifest or install an arbitrary latest dependency wheel.
"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

MAX_MANIFEST_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 32 * 1024**3
PLATFORMS = {"windows-x64": "exe", "linux-amd64": "deb"}
REQUIRED_ARTIFACT_KEYS = {
    "platform",
    "kind",
    "format",
    "version",
    "filename",
    "url",
    "sha256",
    "size_bytes",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


def validate_https_url(value):
    """Accept direct public HTTPS object URLs, without credentials or query tokens."""
    _require(isinstance(value, str) and 1 <= len(value) <= 2048, "Invalid download URL")
    _require(re.fullmatch(r"[A-Za-z0-9._~:/%+\-]+", value) is not None, "Invalid download URL characters")
    parsed = urlsplit(value)
    _require(parsed.scheme == "https" and parsed.hostname, "Download URL must use HTTPS")
    _require(parsed.username is None and parsed.password is None, "Download URL cannot contain credentials")
    _require(not parsed.query and not parsed.fragment, "Download URL must be immutable without query or fragment")
    _require(parsed.port in (None, 443), "Download URL must use port 443")
    _require(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?", parsed.hostname) is not None,
        "Invalid download hostname",
    )
    for segment in parsed.path.split("/"):
        decoded = unquote(segment, errors="strict")
        _require(decoded not in (".", ".."), "Download URL cannot traverse directories")
        _require(
            all(character.isascii() and (character.isalnum() or character in "-._~+") for character in decoded),
            "Invalid download object path",
        )
    return value


def validate_artifact(value, expected_platform=None):
    _require(isinstance(value, dict), "Artifact must be an object")
    _require(REQUIRED_ARTIFACT_KEYS <= value.keys(), "Missing artifact fields")
    _require(value.keys() <= REQUIRED_ARTIFACT_KEYS | {"publisher"}, "Unknown artifact fields")
    platform = value["platform"]
    _require(isinstance(platform, str) and platform in PLATFORMS, "Unsupported platform")
    _require(expected_platform is None or platform == expected_platform, "Artifact platform mismatch")
    _require(value["kind"] == "offline-installer", "Online installers must use a pinned offline installer")
    _require(value["format"] == PLATFORMS[platform], "Artifact format mismatch")
    version = value["version"]
    _require(isinstance(version, str) and len(version) <= 128, "Invalid version")
    pattern = r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9]+)*" if platform == "windows-x64" else r"[0-9][A-Za-z0-9.+~\-]*"
    _require(re.fullmatch(pattern, version) is not None, "Invalid version")
    expected_name = (
        f"communityai-{version}-windows-setup.exe" if platform == "windows-x64" else f"communityai_{version}_amd64.deb"
    )
    _require(value["filename"] == expected_name, "Filename must match the pinned platform and version")
    url = validate_https_url(value["url"])
    _require(unquote(urlsplit(url).path.rsplit("/", 1)[-1]) == expected_name, "URL filename mismatch")
    _require(
        isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None,
        "Invalid SHA-256 digest",
    )
    _require(
        type(value["size_bytes"]) is int and 0 < value["size_bytes"] <= MAX_PACKAGE_BYTES,
        "Invalid package byte size",
    )
    if "publisher" in value:
        publisher = value["publisher"]
        _require(
            isinstance(publisher, str)
            and 1 <= len(publisher) <= 128
            and all(character.isprintable() and character not in '\\"' for character in publisher),
            "Invalid publisher label",
        )
    return dict(value)


def validate_release_manifest(value):
    _require(isinstance(value, dict) and set(value) == {"schema_version", "artifacts"}, "Invalid manifest fields")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1, "Unsupported manifest version")
    artifacts = value["artifacts"]
    _require(isinstance(artifacts, dict) and 1 <= len(artifacts) <= len(PLATFORMS), "Invalid artifact inventory")
    return {
        "schema_version": 1,
        "artifacts": {platform: validate_artifact(artifact, platform) for platform, artifact in artifacts.items()},
    }


def load_release_manifest(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    _require(len(raw) <= MAX_MANIFEST_BYTES, "Manifest exceeds the size limit")
    return validate_release_manifest(json.loads(raw, object_pairs_hook=_unique_pairs))


def select_artifact(manifest, platform):
    checked = validate_release_manifest(manifest)
    _require(platform in checked["artifacts"], "Selected platform is absent from the release")
    return checked["artifacts"][platform]


def artifact_from_file(platform, path, version, base_url, publisher=None):
    """Hash the exact offline package once using bounded memory; never alter it."""
    path = Path(path)
    _require(path.is_file() and not path.is_symlink(), "Offline package must be a regular file")
    _require(base_url == base_url.rstrip("/"), "Base URL must not end with a slash")
    validate_https_url(base_url)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            _require(size <= MAX_PACKAGE_BYTES, "Offline package exceeds size limit")
            digest.update(block)
        after = os.fstat(stream.fileno())
    named = path.stat(follow_symlinks=False)
    _require(
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        == (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
        and size == before.st_size,
        "Offline package changed while being hashed",
    )
    artifact = {
        "platform": platform,
        "kind": "offline-installer",
        "format": PLATFORMS.get(platform),
        "version": version,
        "filename": path.name,
        "url": base_url + "/" + quote(path.name, safe="-._~+"),
        "sha256": digest.hexdigest(),
        "size_bytes": size,
    }
    if publisher is not None:
        artifact["publisher"] = publisher
    return validate_artifact(artifact, platform)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "select"):
        command = commands.add_parser(name)
        command.add_argument("manifest", type=Path)
        if name == "select":
            command.add_argument("--platform", choices=PLATFORMS, required=True)
    create = commands.add_parser("create")
    create.add_argument("--base-url", required=True)
    create.add_argument("--artifact", nargs=3, action="append", metavar=("PLATFORM", "FILE", "VERSION"), required=True)
    create.add_argument("--publisher")
    create.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            _require(not args.output.exists(), "Manifest output already exists")
            artifacts = {}
            for platform, path, version in args.artifact:
                _require(platform not in artifacts, "Duplicate artifact platform")
                artifacts[platform] = artifact_from_file(platform, path, version, args.base_url, args.publisher)
            result = validate_release_manifest({"schema_version": 1, "artifacts": artifacts})
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        else:
            result = load_release_manifest(args.manifest)
            if args.command == "select":
                result = select_artifact(result, args.platform)
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(1, f"Release download metadata rejected: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
