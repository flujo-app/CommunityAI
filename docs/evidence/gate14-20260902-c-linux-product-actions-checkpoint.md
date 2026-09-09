# Gate 14 Linux product-action restart checkpoint

Recorded: 2026-09-02 (America/Bogota)

Status: implementation checkpoint only. Gate 14 remains `IN PROGRESS`. This
document is not hardware acceptance evidence and does not authorize a paid run.

## Why this checkpoint exists

The operator requested a harness rebuild/restart while the concrete Gate 14
platform handlers were being implemented. This checkpoint makes the partial
Linux slice reproducible and leaves the Windows half and all physical
qualification work explicitly open.

GitHub Issues are disabled for `flujo-app/CommunityAI`, so [draft PR #26](https://github.com/flujo-app/CommunityAI/pull/26)
is the restart ticket. It carries the remaining implementation, verification,
staging, budget-revalidation, hardware-run, and cleanup checklist.

## Implemented Linux slice

- `gate14_linux_action_transport.py` source-binds and passes the concrete
  product-action helper to the persistent host.
- `gate14_linux_lifecycle_actions.py` executes the exact verified helper bytes,
  lazily creates one product-action session for production `prepare`, carries
  it across the controller challenge into `calibrate`, and delegates exact
  cleanup on success, error, malformed input, or EOF.
- `gate14_linux_product_actions.py` implements package/audit verification,
  fresh warm-cache adoption and digest verification, systemd-owned packaged
  startup, native credential lifecycle, exact policy and automatic-placement
  observations, low-VRAM and unsupported-CPU-power rejection, worker crash
  recovery, operator pause, packaged restart/cache reuse, challenge-bound
  bandwidth/power/schedule suspension calibration, and exact cleanup.
- The transport contract now distinguishes a present handler with missing
  physical inputs (`product-prepare-failed`) from the removed
  `action-handler-unavailable` placeholder.

Normalized source SHA-256 values:

- product actions:
  `7a904b1c4653eb2a392bb64b1f97404beaee3fff9a2102f3c9c7949f0a2aa973`
- persistent action host:
  `495028ae72a6a1c37a8c718356ed7e7bbae437156b0cc8b7328ddb4fce8e1a36`
- action transport:
  `e7247c57dd01498dfbdfe695079e595e5755ab99c4f9dd01c17ad200cd3c8730`
- transport test:
  `1e1f50ffc9f7fe017fbb6d5662bc4f499ab649a93afcaac27ecf64486fb2f612`
- isolated product-action test after restart:
  `93f16dee2d7652bc74dd5c8f80daa1c63c4197d7c40f7b187b44dd00dde4ebcb`

## Verification completed before restart

Using the repository's existing environments:

```text
.venv-cuda/Scripts/python.exe -m py_compile \
  scripts/gate14_linux_product_actions.py \
  scripts/gate14_linux_lifecycle_actions.py \
  scripts/gate14_linux_action_transport.py

.venv-cuda/Scripts/python.exe -m pytest \
  tests/test_gate14_linux_action_transport.py -q
```

Result: `18 passed`. An expanded transport/sequencer/entrypoint run covering
Linux, Windows, the shared lifecycle, and native entrypoint then passed `118`
tests. Black 22.3.0 reports the three implementation files and the focused test
unchanged. An independent helper also AST-parsed the partial Linux implementation
and identified successful product-action coverage as the main missing local proof.

After restart, `tests/test_gate14_linux_product_actions.py` added controlled
Gate 13 and control-API boundaries for the complete prepare/calibrate/cleanup
success path plus failure cleanup both before and after credential creation. The
product-action and transport focus passes `21` tests, the full
`tests/test_gate14_*.py` matrix passes `201`, Black/isort checks pass in the
repository formatter environment, and Python
compilation passes. This remains no-spend software evidence, not a physical pass.

## Deliberately incomplete

- The Linux handler has not run against a real packaged archive, fresh Gate 9
  warm cache, systemd desktop session, L4 device, or physical resource crossing.
- The equivalent concrete Windows `prepare`/`calibrate` handler is not
  implemented.
- Controller-side fresh direct-upstream cache materialization into the
  `gate14-warm-cache` convention still needs to be connected and tested.
- No no-public-IP IAP staging, protected ACL installation, native remote job,
  challenge checkpoint, hardware acceptance, or cleanup run was attempted.
- No readiness gate was marked passed and Gate 15 remains waiting.
- No cloud reservation or resource was created. Spend for this checkpoint is
  USD 0.

## Restart order

1. Rebuild/restart the harness, then re-check the branch, pinned hashes, and
   unrelated dirty worktree entries before editing.
2. Implement the equivalent source-bound Windows product actions and matching
   contract tests.
3. Connect fresh official-source warm-cache materialization and protected
   staging for both platforms.
4. Only after both platform halves are locally runnable, revalidate
   authentication, inventory, quota, current pricing, and the combined USD 100
   ledger. Record a new bounded reservation before any create.
5. Run Windows then Linux sequentially on fresh no-public-IP L4 hosts, verify
   exact cleanup, and only then evaluate Gate 14 acceptance.

Do not work on credits or macOS.
