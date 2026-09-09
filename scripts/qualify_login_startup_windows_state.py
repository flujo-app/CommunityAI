"""Preserve a qualification run's original Run value without importing winreg.

Values use QueryValueEx's ``(data, registry_type)`` pair, or None for absence.
The injected write callback accepts that pair; delete removes only that value.
This guard detects changes observed before restoration and never overwrites an
unrecognized value. Registry reads and writes are not an atomic compare-and-swap.
"""

from copy import deepcopy


class RunStateConflict(RuntimeError):
    """An unrelated value was observed; restoring it would overwrite another edit."""


class RunStateVerificationError(RuntimeError):
    """The observed registry state does not match the required exact value/type."""


class RunValueGuard:
    def __init__(self, read, write, delete):
        self._read, self._write, self._delete = read, write, delete
        self._original = deepcopy(read())
        self._expected = []

    @property
    def original(self):
        return deepcopy(self._original)

    def expect(self, value):
        """Register an exact value this run may leave after a product action."""
        self._expected.append(deepcopy(value))

    def verify(self, value):
        """Read without mutation and require the supplied exact value/type."""
        if self._read() != value:
            raise RunStateVerificationError("The Run value does not match the expected state")

    def restore(self):
        """Restore only a recognized state, then independently verify the original.

        Return True if restoration required a write/delete. The verification
        still runs when that operation raises; an operation error is never
        converted into successful acceptance, even if the original was restored.
        """
        current = self._read()
        changed = current != self._original
        if changed and current not in self._expected:
            raise RunStateConflict("The Run value changed outside this qualification; it was left untouched")
        try:
            if changed:
                if self._original is None:
                    self._delete()
                else:
                    self._write(deepcopy(self._original))
        finally:
            self.verify(self._original)
        return changed
