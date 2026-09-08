# Frozen Linux catalog startup: retained failed first attempt

The first ordinary-user Linux desktop startup replay **failed** its exact
published sequence-2 assertion. This record remains separate from any later
retry. The [sanitized raw result](qwen-catalog-linux-startup-failed-20260908.json)
retains binary/helper hashes, the failure, cleanup results and both subsequent
metadata-only diagnostic outcomes. Private state and raw process logs stay local.
The later [fresh normal desktop retry](qwen-catalog-linux-startup-20260908.md)
passed and has its own record; it does not erase this first failure.

The offscreen frozen desktop reached its authenticated API in 17.153 seconds,
with the two legacy models and a paused worker. The private configuration still
referenced signed catalog sequence 1; no sequence-2 catalog was staged. Its log
reported `Catalog migration failed verification; starting the saved node
configuration`. The helper then failed at the exact catalog assertion. All
recorded runtime processes stopped and the private native credential was removed.

The existing-configuration branch in `NodeLifecycle._ensure_config` emits that
message when its bundled bootstrap child returns nonzero. Its timeout and
unavailable-executable branches emit different messages. This observation rules
out an early authenticated-status race as the explanation: the desktop had
deliberately started the saved configuration. It does not identify why the child
failed, because this branch does not retain the child's stderr.

Two metadata-only invocations of the exact frozen node's `bootstrap
--refresh_if_needed` subsequently succeeded against separate, new copies of the
failed fixture. Both installed exact sequence-2 bytes and returned empty stderr:

| Diagnostic | Result | Seconds |
| --- | --- | ---: |
| CLI with ordinary process environment | Exit 0 | 16.606 |
| CLI with simulated GUI `_internal` loader path and offscreen Qt | Exit 0 | 13.705 |

Neither diagnostic started a GUI, node server, model or sharing worker. The
second explicitly checked that the original config hash stayed unchanged. The
first recorder did not populate its original-file comparison on success, so its
raw field remains `null`; a later inspection confirmed the original remained on
sequence 1. These diagnostics did not reproduce an invalid signed input or the
loader-path hypothesis. Transient transport failure versus other inherited
process state remains unconfirmed. They do not replace normal GUI acceptance.

The [periodic refresh checkpoint](qwen-catalog-periodic-source-20260908.md)
describes the separate source coverage and still-required live newer-sequence
and active-generation replay.
