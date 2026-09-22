"""Opaque, process-local selection tokens for identically named CUDA cards."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Callable

from drift.node import device_binding
from drift.node.hardware_status import MAX_VISIBLE_ACCELERATORS


class GpuSelectionChangedError(ValueError):
    """The displayed physical card no longer matches the requested selection."""


def _visible_cuda_devices() -> tuple[str, ...]:
    try:
        import torch

        if not torch.cuda.is_available():
            return ()
        count = torch.cuda.device_count()
        if type(count) is not int or count < 0:
            return ()
        return tuple(f"cuda:{index}" for index in range(min(count, MAX_VISIBLE_ACCELERATORS)))
    except Exception:
        return ()


class GpuSelectionTokens:
    """Bind a UI choice to a private physical UUID and config revision.

    Names, capacities and ordinals alone cannot distinguish eight identical
    cards. The HMAC includes the UUID without revealing it. Tokens expire when
    configuration changes, hardware mapping changes, or the node restarts.
    This is local selection integrity, not hardware confidentiality attestation.
    """

    def __init__(
        self,
        *,
        devices: Callable[[], tuple[str, ...]] = _visible_cuda_devices,
        identity: Callable[[str], str | None] | None = None,
        live: Callable[[str], bool] | None = None,
    ) -> None:
        self._key = secrets.token_bytes(32)
        self._devices = devices
        self._identity = device_binding._cuda_identity if identity is None else identity
        self._live = device_binding._DEFAULT_LIVENESS_PROBE if live is None else live

    @staticmethod
    def _validate_inputs(device: str, revision: str) -> None:
        if (
            not isinstance(device, str)
            or re.fullmatch(r"cuda:(0|[1-9][0-9]?)", device) is None
            or int(device.split(":")[1]) >= MAX_VISIBLE_ACCELERATORS
            or not isinstance(revision, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None
        ):
            raise GpuSelectionChangedError("GPU selection is invalid; refresh the available GPUs")

    def _token(self, device: str, revision: str) -> str:
        self._validate_inputs(device, revision)
        return self._token_for_identity(device, revision, self._physical_identity(device))

    def _physical_identity(self, device: str) -> str:
        try:
            physical = device_binding.normalize_cuda_uuid(self._identity(device))
            if physical is None or self._live(physical) is not True:
                raise ValueError("unavailable")
        except Exception:
            raise GpuSelectionChangedError("GPU changed or is unavailable; refresh the available GPUs") from None
        return physical

    def _token_for_identity(self, device: str, revision: str, physical: str) -> str:
        message = f"communityai-gpu-selection-v1\0{revision}\0{device}\0{physical}".encode("ascii")
        return "sha256:" + hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def snapshot(self, revision: str) -> dict:
        rows = []
        # Enumeration is bounded before any identity/liveness probes.
        for device in self._devices()[:MAX_VISIBLE_ACCELERATORS]:
            try:
                token = self._token(device, revision)
            except GpuSelectionChangedError:
                continue
            if any(row["device"] == device for row in rows):
                continue
            rows.append({"device": device, "selection_token": token})
        return {"schema_version": 1, "config_revision": revision, "devices": rows}

    def verify(self, device: str, revision: str, token: str) -> None:
        self.verify_identity(device, revision, token)

    def verify_identity(self, device: str, revision: str, token: str) -> str:
        """Return a verified private identity for local duplicate-card admission."""
        self._validate_inputs(device, revision)
        if not isinstance(token, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", token) is None:
            raise GpuSelectionChangedError("GPU selection token is invalid; refresh the available GPUs")
        physical = self._physical_identity(device)
        if not secrets.compare_digest(self._token_for_identity(device, revision, physical), token):
            raise GpuSelectionChangedError("GPU selection changed; refresh the available GPUs")
        return physical

    def verify_enrolled(self, device: str, revision: str, token: str, physical: str) -> None:
        """Close the race between fresh token verification and immutable binding."""
        self._validate_inputs(device, revision)
        normalized = device_binding.normalize_cuda_uuid(physical)
        if (
            normalized is None
            or not isinstance(token, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", token) is None
            or not secrets.compare_digest(self._token_for_identity(device, revision, normalized), token)
        ):
            raise GpuSelectionChangedError("GPU selection changed during enrollment; refresh the available GPUs")
