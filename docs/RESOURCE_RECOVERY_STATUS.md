# Sharing recovery status

The local control API may expose `contribution.recovery` independently of its
worker list and saved sharing choice. It contains only `state`, `reason` and
`retryable`. The node builder supplies
`resource_recovery_status=resource_manager.recovery_snapshot` to `create_node_app`.
This callback must return cached state immediately, without filesystem access,
process inspection or waiting for recovery locks. The manager's background runner
owns recovery work and rechecks; an HTTP status request does not perform recovery.

The fixed contract is:

| State | Reason | Meaning |
| --- | --- | --- |
| `checking` | `checking` | Verification of earlier sharing work is in progress. |
| `ready` | `none` | Recovery permits new admission; all ordinary policy and resource checks still apply. |
| `blocked` | `active_owner` | Earlier work still has an active owner. |
| `blocked` | `cleanup_pending` | Cleanup or its durable completion remains pending. |
| `blocked` | `legacy_state` | Older saved state lacks the evidence required for automatic recovery. |
| `blocked` | `unverifiable_state` | The saved state cannot currently be verified. |
| `blocked` | `unsupported_platform` | This system does not support the required recovery proof. |

`retryable` is a boolean describing whether verification may be retried. It is
never permission to discard a reservation or start a child. The desktop does not
infer a recovery deadline from it or expose a destructive reset action. The
manager's independent runner handles supported automatic rechecks, including
while sharing is off. Missing providers omit the optional object for compatibility.
Malformed/throwing providers produce fixed blocked/unverifiable status while the
healthy node and control API remain available. No raw errors, process identifiers,
generation tokens, paths or private proof records enter this status object.

The desktop validates the exact fields, enum values, state/reason combinations
and boolean type. Checking or blocked recovery prevents new Start/restart actions
before policy or worker mutation. The master Start button is disabled, while Pause
remains available for current sharing intent and ordinary settings remain editable
when paused. Recovery never turns sharing on or clears explicit Pause intent.

The sharing summary retains **Sharing is off** or **Sharing is paused** with a
separate recovery explanation. A blocked older state is not described as routine
model preparation, insufficient memory/storage, or something Pause can repair.
Recovery remains visible with an empty worker list. Existing observed active work
is not relabelled as stopped merely because admission of new work is blocked.

The API provider and desktop display do not prove process-tree death. That remains
the recovery manager's responsibility. A missing PID, a released loading gate, a
normal-looking parent exit or a desktop termination request is insufficient by
itself. Unproven legacy state remains blocked. Neither changing allowances nor
deleting private files is a supported recovery method.

`tests/test_resource_recovery_status.py` covers real local API-to-client decoding,
cached state while independent work is pending, fixed error projection, malformed
optional fields, zero-mutation Start rejection, and off/Pause truth.
`desktop/tests/test_resource_recovery_controls.py` checks the actual Qt labels,
Start guard, reachable Pause/settings and retained Pause after readiness. These
controlled tests do not qualify actual power-loss recovery, native process-tree
proof, hardware behavior, installed platforms or release acceptance.
