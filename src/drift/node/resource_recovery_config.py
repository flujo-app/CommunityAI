"""Explicit, lexical selection of a Linux worker containment profile."""

import os
from pathlib import PurePosixPath


def normalize_worker_cgroup_root(value):
    """Validate an operator-selected path without probing or creating anything.

    Native capability, delegation and identity are checked by the resource
    manager. This deliberately does not infer a hierarchy from a user ID,
    environment variable or a writable directory.
    """
    try:
        value = os.fspath(value)
    except TypeError:
        raise ValueError("worker cgroup root must be an explicit absolute Linux path") from None
    if (
        not isinstance(value, str)
        or not 1 < len(value) <= 4096
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or ".." in value.split("/")
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError("worker cgroup root must be an explicit absolute Linux path")
    return value
