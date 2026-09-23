"""Fixed-profile sidecar for the volunteer engineering bundle.

The parent marker prevents accidental independent worker launches. It is not an
authentication mechanism or a sandbox against another process of the same user.
"""

from __future__ import annotations

import multiprocessing
import os
import re
import stat
import sys
from pathlib import Path
from typing import Sequence

PARENT_PID_ENV = "COMMUNITYAI_VOLUNTEER_PARENT_PID"
PROFILE_ROOT_ENV = "COMMUNITYAI_VOLUNTEER_PROFILE_ROOT"


def _options(
    arguments: Sequence[str], specs: dict[str, int], *, repeatable=()
) -> tuple[list[str], dict[str, list[str]]]:
    """Parse the exact generated contract, without argparse abbreviation/config files."""
    positional: list[str] = []
    parsed: dict[str, list[str]] = {}
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if not isinstance(item, str) or not item or any(ord(char) < 32 for char in item):
            raise ValueError("volunteer arguments must be nonempty printable strings")
        if not item.startswith("-"):
            positional.append(item)
            index += 1
            continue
        option, separator, inline = item.partition("=")
        if option not in specs:
            raise ValueError(f"unsupported volunteer option: {option}")
        if option in parsed and option not in repeatable:
            raise ValueError(f"duplicate volunteer option: {option}")
        count = specs[option]
        values = [inline] if separator else []
        index += 1
        if count == 0 and separator:
            raise ValueError(f"volunteer flag does not accept a value: {option}")
        if count == 1 and not values:
            if index >= len(arguments) or arguments[index].startswith("-"):
                raise ValueError(f"missing volunteer option value: {option}")
            values.append(arguments[index])
            index += 1
        if count == -1:
            while index < len(arguments) and not arguments[index].startswith("-"):
                values.append(arguments[index])
                index += 1
            if not values:
                raise ValueError(f"missing volunteer option values: {option}")
        if any(not value or value.startswith("-") or any(ord(char) < 32 for char in value) for value in values):
            raise ValueError(f"invalid volunteer option value: {option}")
        parsed.setdefault(option, []).extend(values)
    return positional, parsed


def _safe_path(raw: str, *, base: Path, directory: bool = False, required: bool = False) -> Path:
    path = Path(raw).expanduser()
    if ".." in path.parts:
        raise ValueError("volunteer paths must not traverse parent directories")
    if not path.is_absolute():
        path = base / path
    path = Path(os.path.abspath(path))
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
            raise ValueError("volunteer paths must not use symbolic links or junctions")
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("volunteer paths must not use redirected paths")
        if ancestor == path and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("volunteer paths must not use shared hard-linked files")
    if required and not path.is_file():
        raise ValueError("volunteer input file is missing or not a regular file")
    if path.exists() and ((directory and not path.is_dir()) or (not directory and not path.is_file())):
        raise ValueError("volunteer path has the wrong file type")
    return path


def _contained_path(raw: str, profile, *, directory: bool = False, required: bool = False) -> Path:
    path = _safe_path(raw, base=profile.data_dir, directory=directory, required=required)
    if path == profile.root or not path.is_relative_to(profile.root):
        raise ValueError("volunteer paths must remain beneath the fixed profile root")
    return path


def _fixed_options(parsed: dict[str, list[str]], fixed: dict[str, str], profile) -> list[str]:
    result: list[str] = []
    for option, value in fixed.items():
        if option in parsed:
            actual = parsed[option][0]
            if option in ("--config", "--data_dir", "--node_config"):
                actual = str(_contained_path(actual, profile, directory=option == "--data_dir"))
            if actual != value:
                raise ValueError(f"the volunteer sidecar requires its fixed {option}")
        result.extend((option, value))
    return result


def _node_arguments(arguments: Sequence[str], profile) -> list[str]:
    fixed = {
        "--config": str(profile.config_path),
        "--data_dir": str(profile.data_dir),
        "--host": "127.0.0.1",
        "--port": "18081",
        "--control_key_source": "native",
        "--credential_service": profile.credential_service,
        "--credential_account": profile.credential_account,
    }
    flags = ("--pause_sharing_on_start", "--local_inference_cpu_only")
    positional, parsed = _options(
        arguments, {**dict.fromkeys(fixed, 1), **dict.fromkeys(flags, 0), "--worker-cgroup-root": 1}
    )
    if positional:
        raise ValueError("the volunteer node accepts only its fixed configuration")
    cgroup_arguments = []
    if "--worker-cgroup-root" in parsed:
        from drift.node.resource_recovery_config import normalize_worker_cgroup_root

        cgroup_arguments = ["--worker-cgroup-root", normalize_worker_cgroup_root(parsed["--worker-cgroup-root"][0])]
    return [*_fixed_options(parsed, fixed, profile), *flags, *cgroup_arguments]


def _bootstrap_arguments(arguments: Sequence[str], profile) -> list[str]:
    fixed = {"--data_dir": str(profile.data_dir), "--node_config": str(profile.config_path)}
    positional, parsed = _options(arguments, {**dict.fromkeys(fixed, 1), "--refresh_if_needed": 0})
    if len(positional) != 1:
        raise ValueError("volunteer bootstrap requires exactly one explicit trust input")
    source = _safe_path(positional[0], base=Path.cwd(), required=True)
    return [
        "bootstrap",
        str(source),
        *_fixed_options(parsed, fixed, profile),
        *(("--refresh_if_needed",) if "--refresh_if_needed" in parsed else ()),
    ]


_SERVER_SCALARS = (
    "--model_manifest",
    "--identity_path",
    "--throughput",
    "--num_blocks",
    "--block_indices",
    "--expected_manifest_digest",
    "--expected_block_indices",
    "--expected_artifact_bytes",
    "--expected_artifact_set_digest",
    "--expected_cache_root",
    "--device",
    "--cache_dir",
    "--max_disk_space",
    "--max_device_memory",
    "--max_processing_percent",
    "--processing_budget_path",
    "--port",
    "--public_ip",
    "--revocation_file",
)
_SERVER_PATHS = (
    "--model_manifest",
    "--identity_path",
    "--expected_cache_root",
    "--cache_dir",
    "--processing_budget_path",
    "--revocation_file",
)


def _worker_arguments(mode: str, arguments: Sequence[str], profile) -> list[str]:
    if os.environ.get(PARENT_PID_ENV) != str(os.getppid()) or os.environ.get(PROFILE_ROOT_ENV) != str(profile.root):
        raise ValueError("volunteer workers require the current supervisor parent and fixed profile root")
    if mode == "server":
        positional, parsed = _options(
            arguments,
            {**dict.fromkeys(_SERVER_SCALARS, 1), "--initial_peers": -1, "--announce_maddrs": -1},
            repeatable=("--revocation_file",),
        )
        if len(positional) != 1 or not re.fullmatch(
            r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*", positional[0]
        ):
            raise ValueError("volunteer workers require a manifested repository identity, not a local model path")
        required = (
            "--model_manifest",
            "--identity_path",
            "--initial_peers",
            "--throughput",
            "--device",
            "--max_processing_percent",
        )
        if any(option not in parsed for option in required) or ("--num_blocks" in parsed) == (
            "--block_indices" in parsed
        ):
            raise ValueError("volunteer server arguments do not match a supervised worker launch")
        for option in _SERVER_PATHS:
            if option in parsed:
                parsed[option] = [
                    str(
                        _contained_path(
                            value,
                            profile,
                            directory=option in ("--cache_dir", "--expected_cache_root"),
                            required=option in ("--model_manifest", "--revocation_file"),
                        )
                    )
                    for value in parsed[option]
                ]
        # Unbound/manual configured workers otherwise let configargparse read a
        # default config.yml. Never let that file widen this explicit contract.
        config = profile.data_dir / "config.yml"
        if os.path.lexists(config):
            raise ValueError("remove the implicit config.yml from the volunteer node directory")
    else:
        positional, parsed = _options(
            arguments,
            {
                "--manifest_stdin_sha256": 1,
                "--cache_dir": 1,
                "--no_token": 0,
                "--max_resumptions": 1,
                "--require_direct_upstream": 0,
                "--output": 1,
            },
        )
        if (
            len(positional) > 1
            or bool(positional) == ("--manifest_stdin_sha256" in parsed)
            or "--cache_dir" not in parsed
        ):
            raise ValueError("volunteer acquisition requires one exact manifest and a private cache")
        if positional:
            positional = [str(_contained_path(positional[0], profile, required=True))]
        if "--manifest_stdin_sha256" in parsed and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", parsed["--manifest_stdin_sha256"][0]
        ):
            raise ValueError("volunteer acquisition requires an exact manifest digest")
        for option in ("--cache_dir", "--output"):
            if option in parsed:
                parsed[option] = [str(_contained_path(parsed[option][0], profile, directory=option == "--cache_dir"))]
        parsed["--no_token"] = []
    result = [mode, *positional]
    for option, values in parsed.items():
        if option == "--revocation_file":
            for value in values:
                result.extend((option, value))
        else:
            result.extend((option, *values))
    return result


def _anchor_controller(layout, *, initialize=False):
    """Trusted fixed launcher wiring, invoked only after live service proof."""
    from communityai_desktop.profiles import VolunteerProfile
    from drift.node import linux_anchor as anchor
    from drift.node.linux_anchor_node import AnchorNode
    from drift.node.linux_anchor_state import _sync_directory

    anchor._require(sys.platform.startswith("linux") and getattr(sys, "frozen", False) is True)
    executable = _safe_path(sys.executable, base=Path.cwd(), required=True)
    anchor._require(executable.name == "CommunityAI-Node")
    layout.validate()
    profile = VolunteerProfile.for_current_user()
    if initialize:
        # This exact provisioning mode is explicit first-install authority,
        # never selected automatically from missing marker/state. Existing
        # profiles need a separate checked migration, not deletion or adoption.
        _safe_path(str(profile.root), base=Path.cwd(), directory=True)
        # The only missing ancestor allowed here is the fixed .communityai
        # directory. Persist its entry in the already existing user home.
        _safe_path(str(profile.root.parent.parent), base=Path.cwd(), directory=True)
        anchor._require(profile.root.parent.parent.is_dir())
        home_info = profile.root.parent.parent.stat()
        profile.root.parent.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(profile.root.parent.parent, (home_info.st_dev, home_info.st_ino))
        parent_identity = anchor._private_directory(profile.root.parent)
        profile.root.mkdir(mode=0o700)
        anchor._private_directory(profile.root)
        anchor._require(not os.listdir(profile.root))
        _sync_directory(profile.root.parent, parent_identity)

    def launch(worker_root):
        profile.prepare()
        environment = os.environ.copy()
        for name, value in profile.child_environment().items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        for name in (PARENT_PID_ENV, PROFILE_ROOT_ENV, "HUGGINGFACEHUB_API_TOKEN", "DRIFT_DOWNLOAD_PROGRESS"):
            environment.pop(name, None)
        return (
            [str(executable), *_node_arguments(["--worker-cgroup-root", worker_root], profile)],
            environment,
            str(profile.data_dir),
        )

    return AnchorNode(layout, profile.root, launch, initialize=initialize)


def main(argv: Sequence[str] | None = None) -> int:
    # This must precede profile imports, state validation, and argv inspection:
    # PyInstaller's multiprocessing children have their own dispatch protocol.
    multiprocessing.freeze_support()
    from communityai_desktop.profiles import VolunteerProfile

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] in (["anchor"], ["anchor-initialize"]):
        if len(arguments) != 1:
            raise ValueError("the volunteer anchor accepts no options")
        from drift.node.linux_anchor import serve_anchor

        return serve_anchor(
            controller_factory=lambda layout: _anchor_controller(layout, initialize=arguments[0] == "anchor-initialize")
        )
    profile = VolunteerProfile.for_current_user()
    diagnostics = (
        ["--self-test"],
        ["server", "--self-test"],
        ["--native-self-test"],
        ["--native-self-test", "--require-cuda"],
        ["--cgroup-extension-self-test"],
        ["--help"],
        ["bootstrap", "--help"],
        ["server", "--help"],
        ["edge-acquire", "--help"],
    )
    if arguments in diagnostics:
        forwarded = arguments
        mode = "diagnostic"
    elif arguments[:1] == ["bootstrap"]:
        forwarded = _bootstrap_arguments(arguments[1:], profile)
        mode = "bootstrap"
    elif arguments[:1] in (["server"], ["edge-acquire"]):
        mode = arguments[0]
        forwarded = _worker_arguments(mode, arguments[1:], profile)
    else:
        mode = "node"
        forwarded = _node_arguments(arguments, profile)
    if mode == "node" and sys.platform.startswith("linux"):
        from drift.node.linux_anchor_entry import NODE_TOKEN_ENV, validate_node_entry

        token = os.environ.pop(NODE_TOKEN_ENV, None)
        worker_root = (
            forwarded[forwarded.index("--worker-cgroup-root") + 1] if "--worker-cgroup-root" in forwarded else None
        )
        validate_node_entry(profile.root, token, worker_root)
    profile.prepare()
    for name, value in profile.child_environment().items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    os.environ.pop("HUGGINGFACEHUB_API_TOKEN", None)
    if mode in ("node", "bootstrap", "diagnostic"):
        os.environ.pop("DRIFT_DOWNLOAD_PROGRESS", None)
    elif os.environ.get("DRIFT_DOWNLOAD_PROGRESS"):
        os.environ["DRIFT_DOWNLOAD_PROGRESS"] = str(_contained_path(os.environ["DRIFT_DOWNLOAD_PROGRESS"], profile))
    if mode == "node":
        os.environ[PARENT_PID_ENV] = str(os.getpid())
        os.environ[PROFILE_ROOT_ENV] = str(profile.root)
    elif mode in ("bootstrap", "diagnostic"):
        os.environ.pop(PARENT_PID_ENV, None)
        os.environ.pop(PROFILE_ROOT_ENV, None)
    previous_argv, previous_directory = sys.argv, Path.cwd()
    try:
        os.chdir(profile.data_dir)
        sys.argv = ["CommunityAI-MultiGPU-Test-Node", *forwarded]
        from launch_node import main as dispatch

        return dispatch()
    finally:
        sys.argv = previous_argv
        os.chdir(previous_directory)


if __name__ == "__main__":
    raise SystemExit(main())
