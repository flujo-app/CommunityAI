"""Build a new, signed APT snapshot from verified CommunityAI packages; never publish it."""

import argparse
import datetime
import email.utils
import gzip
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def build(packages, output, signing_key):
    if not re.fullmatch(r"[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}", signing_key):
        raise ValueError("Use the complete repository signing-key fingerprint")
    if not packages:
        raise ValueError("At least one verified CommunityAI package is required")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    pool = output / "pool/main/c/communityai"
    pool.mkdir(parents=True)
    selected = set()
    for package in packages:
        package = package.resolve(strict=True)
        fields = subprocess.check_output(
            ["dpkg-deb", "--show", "--showformat=${Package}\t${Version}\t${Architecture}", str(package)], text=True
        ).split("\t")
        if len(fields) != 3 or fields[0] != "communityai" or fields[2] != "amd64":
            raise ValueError("Only the CommunityAI amd64 package belongs in this repository")
        version = fields[1]
        if not re.fullmatch(r"[0-9][A-Za-z0-9.+~\-]*", version) or version in selected:
            raise ValueError("Invalid or duplicate package version")
        selected.add(version)
        shutil.copyfile(package, pool / f"communityai_{version}_amd64.deb")
    distribution = output / "dists/alpha"
    index_root = distribution / "main/binary-amd64"
    index_root.mkdir(parents=True)
    index = subprocess.check_output(["apt-ftparchive", "packages", "pool"], cwd=output)
    (index_root / "Packages").write_bytes(index)
    (index_root / "Packages.gz").write_bytes(gzip.compress(index, mtime=0))
    valid_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    options = {
        "Origin": "CommunityAI",
        "Label": "CommunityAI",
        "Suite": "alpha",
        "Codename": "alpha",
        "Architectures": "amd64",
        "Components": "main",
        "Description": "CommunityAI alpha packages",
        "Valid-Until": email.utils.format_datetime(valid_until, usegmt=True),
    }
    command = ["apt-ftparchive"]
    for name, value in options.items():
        command.extend(["-o", f"APT::FTPArchive::Release::{name}={value}"])
    release = distribution / "Release"
    release.write_bytes(subprocess.check_output([*command, "release", "dists/alpha"], cwd=output))
    keyring = output / "communityai-archive-keyring.gpg"
    exported = subprocess.check_output(["gpg", "--batch", "--export", signing_key])
    if not exported:
        raise ValueError("The repository public signing key is unavailable")
    keyring.write_bytes(exported)
    for action, filename in (("--clearsign", "InRelease"), ("--detach-sign", "Release.gpg")):
        target = distribution / filename
        subprocess.run(
            ["gpg", "--batch", "--local-user", signing_key, "--output", str(target), action, str(release)], check=True
        )
        verification = ["gpgv", "--keyring", str(keyring), str(target)]
        if action == "--detach-sign":
            verification.append(str(release))
        subprocess.run(verification, check=True)
    artifacts = {}
    for path in sorted(output.rglob("*")):
        if path.is_file():
            with path.open("rb") as source:
                artifacts[path.relative_to(output).as_posix()] = hashlib.file_digest(source, "sha256").hexdigest()
    (output / "repository.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "signing_key_fingerprint": signing_key.upper(),
                "valid_until": valid_until.isoformat(),
                "sha256": artifacts,
                "published": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signing-key", required=True)
    args = parser.parse_args()
    print(build(args.package, args.output, args.signing_key))
