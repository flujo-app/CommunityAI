# Checked Linux node drain acknowledgement

This component closes one unsafe lifecycle boundary required by the installed
Linux anchor design. A node process no longer reports an ordinary successful
exit when worker resource cleanup is incomplete.

## Contract

- New admission is excluded before the reservation manager closes.
- The complete durable reservation journal must be empty while its native OS
  lock is held. In-memory ownership alone is not sufficient.
- When an explicit worker cgroup root is configured, its aggregate
  `cgroup.events` population bit must prove that the whole descendant tree is
  empty. Empty generation directories are not pruned by this component.
- The node emits exit status 75 when either the supervisor drain or the checked
  reservation-manager close remains pending. A normal status 0 is the exact
  process's cleanup acknowledgement.
- The desktop first asks its owned node to stop through the authenticated local
  control API. This gives Windows, where `Popen.terminate()` is not graceful, a
  status-0 drain path. Process termination and kill remain fail-closed fallbacks.
- Contribution cleanup timeouts are bounded at 300 seconds. The desktop and
  installer use one 3,030-second end-to-end shutdown bound covering transition,
  concurrent contained-worker cleanup, final drain and manager teardown. A
  timeout still fails closed; it never turns a forced stop into an acknowledgement.
- An explicit shutdown request is sticky and overrides a pending or concurrent
  catalog/configuration reload, so the owned process must exit rather than start
  another node generation.
- The desktop accepts only status 0 from its owned process. Status 75, another
  nonzero status, a lost process result or a forced kill remains sticky and
  blocks the update/removal-safe acknowledgement until a later exact node exits
  with status 0 after completing the global drain.

The process handle binds the acknowledgement to the node invocation observed by
its parent. A later persistent anchor must additionally bind the node generation,
service invocation, peer UID, fixed volunteer profile and node cgroup identity.
It must prove the node leaf empty as well as this worker subtree, and must keep
lost replies or anchor crashes pending.

## Limits

This is not installed service ownership. It does not provision or start a
systemd user service, enable lingering, create stable anchor/control/node/worker
subgroups, authorize desktop reconnect, migrate legacy journals or remove empty
cgroup directories. Those remain the next anchor and installer tasks. It also
does not qualify an installed Ubuntu system, GPU, model, power-loss case or full
beta.
