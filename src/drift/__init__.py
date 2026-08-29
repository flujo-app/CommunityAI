import os
import platform

os.environ.setdefault("BITSANDBYTES_NOWELCOME", "1")

if platform.system() == "Linux":
    # Hivemind defaults to Torch's file_system sharing strategy, whose torch_shm_manager
    # can unlink a tensor before its allocator closes and abort route startup in C++.
    # Linux's file_descriptor strategy avoids named-file unlink races; the server already
    # raises its file-descriptor limit before starting multiprocessing workers.
    os.environ.setdefault("HIVEMIND_MEMORY_SHARING_STRATEGY", "file_descriptor")

if platform.system() == "Darwin":
    # Necessary for forks to work properly on macOS, see https://github.com/kevlened/pytest-parallel/issues/93
    os.environ.setdefault("no_proxy", "*")
    os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

import hivemind
import transformers
from packaging.version import parse as _parse_version

from drift.client import *
from drift.models import *
from drift.utils import *
from drift.utils.asyncio import patch_hivemind_task_cleanup as _patch_hivemind_task_cleanup
from drift.utils.logging import initialize_logs as _initialize_logs

__version__ = "2.3.0.dev2"


if not os.getenv("DRIFT_IGNORE_DEPENDENCY_VERSION"):
    assert (
        _parse_version("5.13.0") <= _parse_version(transformers.__version__) < _parse_version("6.0.0")
    ), "Please install a proper transformers version: pip install transformers>=5.13,<6.0"


def _override_bfloat16_mode_default():
    if os.getenv("USE_LEGACY_BFLOAT16") is None:
        hivemind.compression.base.USE_LEGACY_BFLOAT16 = False


_initialize_logs()
_override_bfloat16_mode_default()
_patch_hivemind_task_cleanup()
