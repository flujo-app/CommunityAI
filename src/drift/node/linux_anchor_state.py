"""Compatibility alias for the lightweight shared anchor implementation."""

import sys as _sys

from communityai_anchor import linux_anchor_state as _implementation

_sys.modules[__name__] = _implementation
