"""Run one source-bound Gate 14 packaged lifecycle on its native host."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import gate14_linux_action_transport as linux_transport
import gate14_packaged_lifecycle as lifecycle
import gate14_windows_action_transport as windows_transport

ActionFactory = Callable[[lifecycle.LifecycleConfig], Any]


class Gate14LifecycleEntrypointError(ValueError):
    """The native platform or action adapter failed closed."""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _native_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    raise Gate14LifecycleEntrypointError("unsupported lifecycle platform")


def _factory(platform_name: str) -> ActionFactory:
    if platform_name == "windows":
        return windows_transport.WindowsActionTransport
    if platform_name == "linux":
        return linux_transport.LinuxActionTransport
    raise Gate14LifecycleEntrypointError("lifecycle platform is invalid")


def run_from_config(
    path: Path,
    *,
    action_factory: ActionFactory | None = None,
    native_platform: str | None = None,
) -> Mapping[str, Any]:
    config = lifecycle.load_config(Path(path))
    observed_platform = _native_platform() if native_platform is None else native_platform
    if observed_platform not in {"windows", "linux"} or config.platform != observed_platform:
        raise Gate14LifecycleEntrypointError("lifecycle platform binding changed")
    factory = action_factory or _factory(config.platform)
    actions = factory(config)
    if not (
        callable(getattr(actions, "prepare", None))
        and callable(getattr(actions, "calibrate", None))
        and callable(getattr(actions, "cleanup", None))
        and callable(getattr(actions, "close", None))
    ):
        raise Gate14LifecycleEntrypointError("lifecycle action adapter is invalid")
    try:
        return lifecycle.run_lifecycle(config, actions)
    finally:
        actions.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", required=True)
    try:
        arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
        document = run_from_config(Path(arguments.config))
        print(_canonical(document))
        return 0
    except (Exception, SystemExit):
        print(
            _canonical(
                {
                    "failure_code": "gate14_lifecycle_failed",
                    "result": "failed",
                    "schema_version": 1,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
