"""Stable worker exit signal for a budget that cannot admit the selected blocks."""

DEVICE_MEMORY_BUDGET_EXIT_CODE = 78


class DeviceMemoryBudgetError(ValueError):
    """The selected blocks cannot fit within the configured device-memory ceiling."""
