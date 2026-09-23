"""Compatibility alias for the lightweight shared anchor implementation."""

import sys as _sys

from communityai_anchor import resource_recovery_config as _implementation

_sys.modules[__name__] = _implementation
