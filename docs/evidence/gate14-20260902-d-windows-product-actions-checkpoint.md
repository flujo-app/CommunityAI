# Gate 14 Windows product-action checkpoint

Recorded: 2026-09-02 (America/Bogota)

Status: implementation checkpoint only. Gate 14 remains `IN PROGRESS`. This
document is not hardware acceptance evidence and does not authorize a paid run.

## Result

The Windows lifecycle bridge now has a concrete, persistent packaged-product
`prepare`/`calibrate`/`cleanup` implementation equivalent to the verified
Linux half. The implementation is fail-closed and was developed on top of source
`30891396916e61071492cfa056c15d1f4d94e547`.

No GCP, Fly.io, or GitHub provider resource was created or changed while
implementing or verifying this checkpoint. No reservation was recorded and
spend was USD 0 under the combined USD 100 ceiling.

## Implemented Windows slice

- The action transport opens and retains Windows read handles that deny source
  write/delete, verifies normalized source digests, and carries the verified
  lifecycle configuration handle across the complete persistent session.
- The product action audits and installs the exact production package, runs the
  packaged self-tests, adopts only the exact controller-bound warm-cache
  inventory, starts the real desktop/node path, and creates exactly one native
  control credential only after startup is ready.
- Initial and restart launches use the action-specific persistent node
  configuration, node-data directory, and packaged bootstrap configuration.
  No implicit user-profile node path may satisfy the contract.
- The handler verifies the exact sharing policy, remotely acknowledged automatic
  placement, low-VRAM rejection, unsupported CPU-power behavior, crash recovery,
  operator pause, packaged restart, and cache reuse.
- Bandwidth, physical-power, and schedule calibrations remain bound to the
  controller challenge and verify limit crossing, worker absence, preserved
  owner intent, and below-limit recovery.
- Cache inventory and deletion use native no-follow handles for the root and
  every descendant. File identity, exact path/count/bytes/digest, and reparse
  rejection are proved while locks remain held through drills.
- Native Job Object helpers prove membership and terminate only exact members.
  Failed Job or power-burn cleanup retains its owner handle for retry.
- Cleanup is phased in process/burn, credential, cache-lock, and root order.
  A failed phase preserves the package deletion tool and roots; one-shot process,
  burn, and credential failures are each proved to succeed on a second cleanup.

## Source bindings

Normalized source SHA-256 values used by the persistent transport:

- Gate 13 Windows packaged lifecycle:
  `aa549335b63f43ef2e68f40881635ab077e916878bc472b8674424aa087a6dda`
- Gate 13 Windows packaged inference:
  `2d53424c886ff4a70367a3a0844e33a234bc6c290828a21b70a134b5bf115611`
- Windows product actions:
  `3a29f13ecd855fbdb21d42b21ffd3e793e8a3c1086f816a28d20f9e8cfbb2e23`
- Persistent Windows action host:
  `4ebf68d5fbeb3afad9cd52a7e062162de61da4f6ecee0cd113585a20c84fdab5`

The final Windows action transport content SHA-256 is
`ebe455967082c2ee8f93b499d9b73aff92854bd0a625a85527c038328c24abfc`.
The isolated product-action test content SHA-256 is
`7598606087777f78f1bf6799e39a85f1e0dc503efc0ba2b5b1484524983681f0`.

## Verification

Using the repository's existing environments:

```text
.venv-cuda/Scripts/python.exe -m pytest \
  tests/test_gate14_windows_product_actions.py \
  tests/test_gate14_windows_action_transport.py -q
# 50 passed

.venv-cuda/Scripts/python.exe -m pytest \
  tests/test_gate13_windows_packaged_lifecycle.py -q
# 16 passed

.venv-cuda/Scripts/python.exe -m pytest tests/test_gate14_*.py -q
# PowerShell-expanded file list: 211 passed
```

PowerShell parsing passed for the Gate 13 lifecycle, Gate 14 product actions,
and persistent action host. Black, isort, Python compilation, and
`git diff --check` passed. An independent adversarial review reran the
50-test Windows focus and 16-test Gate 13 regression and returned PASS after
confirming the retryable power-burn cleanup invariant.

## Deliberately incomplete

- Neither platform handler has executed this checkpoint against the retained
  production archives on fresh clean hosts.
- The historical Gate 9 physical caches were cleaned. Fresh direct
  official-source cache materialization, its exact artifact record, and
  controller-side staging are still required for both platform profiles.
- No no-public-IP Windows or Linux L4 host was created, no hardware calibration
  was attempted, and no Gate 14 acceptance pass is claimed.
- Native authentication, inventory, quota, pricing, and the current-epoch
  ledger must be revalidated immediately before any new bounded reservation.
- Gate 14 remains `IN PROGRESS`; Gate 15 remains waiting.

## Next unblocked gate

Connect and test fresh direct official-source cache materialization for the
exact Windows/Qwen and Linux/Gemma profiles. Do not create paid hosts until
that source-bound input is runnable. Then revalidate the provider and USD 100
budget boundaries, reserve a bounded amount, run Windows then Linux
sequentially, and prove exact cleanup.

Do not work on credits or macOS.
