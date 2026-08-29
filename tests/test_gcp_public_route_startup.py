import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STARTUP_PATH = REPOSITORY_ROOT / "scripts" / "gcp_public_route_startup.sh"


def test_bootstrap_is_lf_only_bounded_and_shell_valid():
    payload = STARTUP_PATH.read_bytes()

    assert payload.startswith(b"#!/usr/bin/env bash\n")
    assert not payload.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in payload
    assert len(payload) < 16_384
    bash_candidates = [shutil.which("bash")]
    git = shutil.which("git")
    if git is not None:
        bash_candidates.append(str(Path(git).resolve().parents[1] / "bin" / "bash.exe"))
    failures = []
    for bash in dict.fromkeys(candidate for candidate in bash_candidates if candidate):
        completed = subprocess.run(
            [bash, "-n", str(STARTUP_PATH)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode == 0:
            break
        failures.append(completed.stderr)
    else:
        pytest.skip("a working bash is unavailable: " + " ".join(failures))


def test_bootstrap_pins_every_external_runtime_boundary():
    script = STARTUP_PATH.read_text(encoding="utf-8")

    expected = {
        "installer_sha256": "876d7d02e3e1166c105bb0a9148993c3ea9b789a041f78143c928e7ab317c14f",
        "driver_version": "570.211.01",
        "ca_certificates_version": "20260601~24.04.1",
        "curl_version": "8.5.0-2ubuntu10.13",
        "gnupg_version": "2.4.4-2ubuntu17.4",
        "docker_version": "29.1.3-0ubuntu3~24.04.2",
        "containerd_version": "2.2.1-0ubuntu1~24.04.3",
        "toolkit_version": "1.20.0-1",
        "toolkit_key_sha256": "c880576d6cf75a48e5027a871bac70fd0421ab07d2b55f30877b21f1c87959c9",
        "toolkit_list_sha256": "bc6cb10d15243cfcf5c1506c46d7fed4037ce16f84a3f592482cb4ac874c528f",
    }
    for name, value in expected.items():
        assert f"readonly {name}='{value}'" in script
    assert "?generation=1785935286399764'" in script
    assert script.count("sha256sum --check --strict") == 3
    assert 'install_driver --force-version "${driver_version}"' in script
    assert '"docker.io=${docker_version}"' in script
    assert '"containerd=${containerd_version}"' in script
    for package in (
        "libnvidia-container1",
        "libnvidia-container-tools",
        "nvidia-container-toolkit-base",
        "nvidia-container-toolkit",
    ):
        assert f'"{package}=${{toolkit_version}}"' in script
    assert "nvidia-ctk runtime configure --runtime=docker --set-as-default" in script
    assert "docker info --format" in script
    assert """test "$(docker info --format '{{.DefaultRuntime}}')" = 'nvidia'""" in script
    assert """grep -q '"nvidia"'""" in script


def test_bootstrap_is_idempotent_fail_closed_and_does_not_launch_routes():
    script = STARTUP_PATH.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "umask 077" in script
    assert 'if [[ "${EUID}" -ne 0 ]]' in script
    assert 'if [[ "${installed_driver}" != "${driver_version}" ]]' in script
    assert 'rm -f "${ready_path}" "${temporary_ready}"' in script
    assert script.index('rm -f "${ready_path}"') < script.index("apt-get update")
    assert 'trap \'rm -f "${installer_path}" "${key_path}" "${list_path}" "${temporary_ready}"\' EXIT' in script
    assert 'test "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader)"' in script
    assert "systemctl is-active --quiet containerd" in script
    assert "systemctl is-active --quiet docker" in script
    assert "apt-mark hold containerd docker.io" in script
    assert "libnvidia-container1 \\" in script
    assert "docker pull" not in script
    assert "docker run" not in script
    assert "gcloud " not in script
    assert "gh " not in script
    assert "flyctl" not in script
    assert "curl |" not in script
    assert re.search(r"curl[^\\n]*\\|\\s*(?:ba)?sh", script) is None


def test_bootstrap_readiness_is_private_bounded_aggregate_only():
    script = STARTUP_PATH.read_text(encoding="utf-8")

    match = re.search(r"""\n  '(\{"container_runtime".+\})' \\\n  > """, script)
    assert match is not None
    readiness = match.group(1)
    assert len(readiness.encode()) < 1024
    assert '"ready":true' in readiness
    assert '"schema_version":1' in readiness
    assert '"scope":"communityai-public-route-bootstrap"' in readiness
    assert "path" not in readiness.lower()
    assert "token" not in readiness.lower()
    assert "peer" not in readiness.lower()
    assert "address" not in readiness.lower()
    assert "credential" not in readiness.lower()
    assert 'chmod 0600 "${temporary_ready}"' in script
    assert 'mv -f "${temporary_ready}" "${ready_path}"' in script


def test_bootstrap_contains_no_embedded_credential_or_private_endpoint():
    script = STARTUP_PATH.read_text(encoding="utf-8")
    lowered = script.lower()

    assert "authorization:" not in lowered
    assert "bearer " not in lowered
    assert "password=" not in lowered
    assert "token=" not in lowered
    assert "private_key" not in lowered
    assert "metadata.google.internal" not in lowered
    assert not re.search(r"(?i)(?:ghp_|github_pat_|flyv1\s|ya29\.)", script)
    assert hashlib.sha256(STARTUP_PATH.read_bytes()).hexdigest() != "0" * 64
