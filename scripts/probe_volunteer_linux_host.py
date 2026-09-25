"""Read-only Ubuntu volunteer inventory for anchor setup and exact-model trials.

Runs no installer, model download, backend, GPU workload or configuration
change. The report omits hostnames, user names, GPU UUIDs and storage paths.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from probe_deepseek_h100_host import make_report as make_h100_report
from probe_deepseek_h100_host import parse_gpu_inventory, probe as probe_h100

_MAX_OUTPUT = 16_384
_UNIT = "communityai-multigpu-anchor.service"
_PROPERTIES = ("Id", "LoadState", "Delegate", "Type", "KillMode", "Restart", "Transient")
_REQUIRED = {
    "Id": _UNIT,
    "LoadState": "loaded",
    "Delegate": "yes",
    "Type": "exec",
    "KillMode": "control-group",
    "Restart": "no",
    "Transient": "no",
}
_GLM_W4A8_ARTIFACT = "gpustack/GLM-5.3-W4A8"
_GLM_W4A8_REVISION = "f6d1e50d43edb5fb3f3141f19fc691511a50756c"
_GLM_W4A8_WEIGHT_BYTES = 399_716_726_536
_GLM_W4A8_RECIPE_GPU_BYTES = 447_000_000_000
_GLM_W4A8_PER_RANK_BYTES = (_GLM_W4A8_RECIPE_GPU_BYTES + 7) // 8


def _exact_properties(payload: str, keys: tuple[str, ...]) -> dict[str, str] | None:
    if type(payload) is not str or len(payload) > _MAX_OUTPUT:
        return None
    result = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in keys or key in result:
            return None
        result[key] = value
    return result if set(result) == set(keys) else None


def _cgroup2_root_observed(payload: str) -> bool:
    if type(payload) is not str or len(payload) > 2_000_000:
        return False
    for line in payload.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if separator and len(fields) >= 5 and fields[4] == "/sys/fs/cgroup" and after.split()[:1] == ["cgroup2"]:
            return True
    return False


def _run(command: list[str]) -> str | None:
    runtime = f"/run/user/{os.geteuid()}"
    try:
        result = subprocess.run(
            command,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "XDG_RUNTIME_DIR": runtime,
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + runtime + "/bus",
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="strict",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return result.stdout if result.returncode == 0 and len(result.stdout) <= _MAX_OUTPUT else None


def systemd_readiness(uid: int, mountinfo: str, *, run=_run) -> dict[str, object]:
    if type(uid) is not int or uid < 0:
        raise ValueError("invalid user identity")
    linger = run(["/usr/bin/loginctl", "show-user", str(uid), "--property=Linger"])
    linger_values = None if linger is None else _exact_properties(linger, ("Linger",))
    service = run(["/usr/bin/systemctl", "--user", "show", _UNIT, "--property=" + ",".join(_PROPERTIES)])
    service_values = None if service is None else _exact_properties(service, _PROPERTIES)
    return {
        "ordinary_user": uid != 0,
        "cgroup2_root_observed": _cgroup2_root_observed(mountinfo),
        "linger": "unavailable" if linger_values is None else linger_values["Linger"],
        "user_service_query_available": service_values is not None,
        "anchor_unit_loaded": service_values is not None and service_values["LoadState"] == "loaded",
        "anchor_unit_policy_matches": service_values == _REQUIRED,
        "delegated_cgroup_verified": False,
        "required_syscalls_verified": False,
        "installed_anchor_verified": False,
    }


def glm_w4a8_h100_preflight(h100: dict[str, object]) -> dict[str, object]:
    """Reuse the read-only GPU report for a distinct quantized GLM candidate."""
    if type(h100) is not dict or h100.get("scope") != "deepseek-h100-host-diagnostic":
        raise ValueError("invalid H100 report")
    gpus = h100.get("gpus")
    checks = h100.get("necessary_checks")
    if type(gpus) is not list or type(checks) is not dict:
        raise ValueError("invalid H100 inventory")
    free = [gpu.get("free_bytes") for gpu in gpus if type(gpu) is dict]
    if len(free) != len(gpus) or any(type(amount) is not int or amount < 0 for amount in free):
        raise ValueError("invalid H100 free memory")
    storage = h100.get("storage_free_bytes")
    if type(storage) is not int or storage < 0:
        raise ValueError("invalid model storage")
    return {
        "artifact_id": _GLM_W4A8_ARTIFACT,
        "artifact_revision": _GLM_W4A8_REVISION,
        "base_model_id": "zai-org/GLM-5.3",
        "variant": "third-party-W4A8",
        "necessary_checks": {
            "eight_h100_80gb_class": checks.get("eight_h100_80gb_class") is True,
            "each_gpu_free_at_least_recipe_eighth": len(free) == 8 and all(
                amount >= _GLM_W4A8_PER_RANK_BYTES for amount in free
            ),
            "aggregate_gpu_free_at_least_recipe_minimum": sum(free) >= _GLM_W4A8_RECIPE_GPU_BYTES,
            "storage_free_at_least_pinned_weight_bytes": storage >= _GLM_W4A8_WEIGHT_BYTES,
        },
        "runtime_fit_proven": False,
        "quality_equivalence_proven": False,
        "remote_code_reviewed": False,
        "rights_approved": False,
        "backend_qualified": False,
    }


def probe(storage_path: Path) -> dict[str, object]:
    if not sys.platform.startswith("linux"):
        raise ValueError("Linux is required")
    h100 = probe_h100(storage_path)
    mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    libc_name, libc_version = platform.libc_ver()
    return {
        "schema_version": 1,
        "scope": "communityai-volunteer-linux-host-diagnostic",
        "kernel_release": os.uname().release,
        "libc": {"name": libc_name, "version": libc_version},
        "anchor_prerequisites": systemd_readiness(os.geteuid(), mountinfo),
        "deepseek_h100": h100,
        "glm53_w4a8_h100": glm_w4a8_h100_preflight(h100),
        "installation_qualified": False,
        "model_qualified": False,
    }


def self_test() -> None:
    mount = "25 1 0:23 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
    assert _cgroup2_root_observed(mount)
    assert not _cgroup2_root_observed(mount.replace("cgroup2", "cgroup"))
    assert _exact_properties("Linger=yes\n", ("Linger",)) == {"Linger": "yes"}
    assert _exact_properties("Linger=yes\nLinger=no\n", ("Linger",)) is None

    def run(command):
        if command[0].endswith("loginctl"):
            return "Linger=yes\n"
        return "\n".join(f"{key}={value}" for key, value in _REQUIRED.items()) + "\n"

    ready = systemd_readiness(1000, mount, run=run)
    assert ready["ordinary_user"] and ready["cgroup2_root_observed"] and ready["anchor_unit_policy_matches"]
    assert ready["delegated_cgroup_verified"] is False and ready["installed_anchor_verified"] is False
    missing = systemd_readiness(1000, mount, run=lambda _: None)
    assert missing["linger"] == "unavailable" and missing["anchor_unit_loaded"] is False
    sample = "\n".join(f"{i}, NVIDIA H100 80GB HBM3, 81559, 70000, 570.133.20" for i in range(8))
    h100 = make_h100_report(parse_gpu_inventory(sample), 250_000_000_000,
                             _GLM_W4A8_WEIGHT_BYTES, "ubuntu", "20.04")
    glm = glm_w4a8_h100_preflight(h100)
    assert all(glm["necessary_checks"].values()) and glm["backend_qualified"] is False
    h100["gpus"][0]["free_bytes"] = _GLM_W4A8_PER_RANK_BYTES - 1
    assert not glm_w4a8_h100_preflight(h100)["necessary_checks"]["each_gpu_free_at_least_recipe_eighth"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-path", type=Path, default=Path.cwd(), help="existing intended model volume")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("volunteer Linux host diagnostic self-test PASS")
        return 0
    try:
        result = probe(args.storage_path)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"diagnostic failed: {exc}\n")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
