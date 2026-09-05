# Gate 13 Linux route-worker failure, 2026-09-05

Run: `g13-20260905-152936-2b9a`. Inspection used read-only SSH,
journal/process listings and authenticated localhost GETs. No VM process,
service or metadata was changed; no cloud run or desktop build was dispatched.

## Observed

- Windows qualification passed in 375.797 seconds.
- Linux startup and its initial UI session passed. Initial inference returned
  one token with all 35 Gemma blocks available (181.338214 seconds).
- The restarted Linux UI waited in `wait_ready`. Its node reported fresh
  discovery observations with 0/35 Gemma blocks and no complete route.
- The route's Gemma systemd service stayed active with `NRestarts=0`, but its
  internally supervised worker repeatedly shut down and restarted. At 16:41:14
  UTC it announced its blocks offline and reported a shutdown signal.
- Subsequent workers reported that the configured worker identity was already
  taken. A surviving DHT process (PID 21636, PPID 1) still owned p2pd PID 21652
  using that same identity and public port 31338. The client also had multiple
  orphaned worker descendants.
- The restarted UI eventually progressed but failed inference with HTTP 500
  after 1277.018252 seconds. The launcher collected this in
  `linux-host-job-failure-output.json`, then deleted its run-scoped resources.
  Final cleanup passed; the protected bootstrap remained running.

## Fix

`WorkerLaunch` equality previously included `placement_reason`. After the
planner's 15-minute residency, a fresh observation changes that explanation
(for example, minimum replicas 0 to 1) without changing the model or range.
The reconciler therefore stopped and replaced an unchanged assignment. The
regression test reproduces this with the real planner at timestamps 0 and 901.
Explanatory text is now excluded from launch equality; commands, resource
limits, admission and exact artifact bindings remain compared.

On Linux, workers now own separate process sessions. Both graceful/forced
stops and observed worker exits kill any remaining members of that worker's
process group before allowing replacement, releasing stale DHT identities and
ports. Windows process creation and termination retain their previous path.

The source fix was also backported onto the existing pinned route source:
`d2c93af74311bddbee516b5f35e65789449b8b07`. The replacement local wheel is
389449 bytes, SHA-256
`edfd4598c293719d4d7701c9613b64f47f9fd20c3a2dc2e4c0fcacacad3c493a`.
Comparison with the previous wheel finds only `worker_supervisor.py` and its
wheel RECORD changed after normalizing line endings. The old wheel is retained.
The one-click config and pinned setup helper both select the replacement.

## Validation and limits

- Worker tests: 31 passed on Windows, including the planner reproduction and
  mocked Linux/Windows graceful-stop, forced-stop and crash cleanup paths.
- Two real Linux descendant-port-release tests are present but skipped on this
  Windows host; they were not represented as Linux execution.
- Backported route runtime's existing worker suite: 22 passed. Its added
  lifecycle paths also passed the six platform/exit-mode regression cases.
- Gate 13 runner/provider/startup/fence/lifecycle tests: 238 passed.
- No new end-to-end GCP result is claimed. The next normal one-click run needs
  a newly built desktop package to include the client-runtime source fix;
  the replacement route wheel has already been built locally.
