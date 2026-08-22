"""Persistent local-node building blocks.

The node package deliberately stays independent from FastAPI so model lifecycle and
selection can be tested without installing the optional HTTP dependencies.
"""

from drift.node.model_manager import (
    AmbiguousModelError,
    LoadedModel,
    ModelDescriptor,
    ModelInUseError,
    ModelManager,
    ModelManagerClosedError,
    ModelNotFoundError,
    ModelRuntime,
    ModelSnapshot,
    ModelState,
    ModelUnloadError,
)

__all__ = [
    "AmbiguousModelError",
    "LoadedModel",
    "ModelDescriptor",
    "ModelInUseError",
    "ModelManager",
    "ModelManagerClosedError",
    "ModelNotFoundError",
    "ModelRuntime",
    "ModelSnapshot",
    "ModelState",
    "ModelUnloadError",
]
