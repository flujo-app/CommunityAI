"""Optional native Linux birth primitive; configured cgroups require its presence."""

import sys

from setuptools import Extension, setup

setup(
    ext_modules=(
        [Extension("drift.node._linux_cgroup_spawn", ["src/drift/node/_linux_cgroup_spawn.c"], optional=True)]
        if sys.platform.startswith("linux")
        else []
    ),
    # Keep the C source in sdists produced on either supported build platform.
    package_data={"drift.node": ["_linux_cgroup_spawn.c"]},
)
