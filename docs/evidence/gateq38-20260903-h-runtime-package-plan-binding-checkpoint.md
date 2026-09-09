# Gate Q3.8 runtime-package plan-binding checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 controller/source contract; Gate Q3.8 remains IN PROGRESS
Source commit: `50dd0a3daf64de4a76afe0d51de2110f09804450`
Source tree: `406d11213abc2ce427eed4f1e130c7e86763b43d`

## Scope

This checkpoint removes the circular dependency in which creating the strict Linux
runtime-package record required the final route plan that was itself supposed to bind
that record. The package stage now consumes a narrow controller-protected source
context. The route controller independently validates the resulting complete package
record and carries it immutably through every paid-action identity.

This is a controller/source-contract result. It does not stage or execute the
production package on a native Linux qualification host.

## Non-circular source and package contract

The source context has one exact schema and scope and binds the source commit, source
tree, and the controller's complete sorted required-source set. The stage command:

- accepts `--source-context` and no longer accepts the final plan or a separate
  source-tree claim;
- parses bounded JSON with duplicate-key rejection, checks the context parent and
  file as controller-protected inputs, then requires an exact reread;
- verifies the imported package-stage and desktop-release verifier modules against
  the exact source bindings before using them;
- emits a package record that includes the exact source-binding-set digest; and
- rejects an output path that aliases the manifest or source context, or resolves
  beneath the source or extracted-release roots, before validation can modify it.

The controller is the single validator for the complete package-record schema. It
requires exact platform, archive, node-root and executable identities; exact digest,
size, inventory, source-commit, manifest, and source-binding claims; and a canonical
self-digest computed over the record without that digest field using the package
domain's required trailing newline. That package digest remains deliberately distinct
from the route controller's stable-plan digest domain.

## Route and authorization binding

A validated package record and the source/authorization mappings are stored as
immutable mappings. The full record is included in:

- the stable route-plan digest;
- the execution-inventory digest;
- every provider action record;
- every non-null action ID through the plan digest; and
- reservation and preflight comparisons through the plan and execution-inventory
  digests.

Changing any package or source-binding field, even with a correctly recomputed package
self-digest, therefore creates a distinct plan. Authorization or preflight evidence
for the former plan cannot be reused.

## Verification

Checks against the exact committed candidate passed:

- `156 passed, 1 skipped` in the focused route-controller and package-stage suite;
- `184 passed, 1 skipped` across all `tests/test_gateq38_*.py`;
- `205 passed, 1 skipped` across the adjacent package, controller, GCP-adapter, and
  desktop-builder matrix;
- `1,733 passed, 11 skipped` in the repository offline unit matrix;
- Black, isort, Python compilation, and Git whitespace checks; and
- independent adversarial source review plus an independent frozen-index security
  and test review, both returning PASS.

The native-POSIX skip is expected on this Windows verification host. No cloud
provider operation, archive or model download, reservation, or resource mutation was
performed.

## Canonical committed blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_route_controller.py` | 94,450 | `6c817c03e60f45216ab3d875fcb0f29ab48fc963997cbf0ac2471a53b7b07cb6` |
| `scripts/gateq38_stage_package.py` | 25,046 | `c08a630c8bd1f1eb40ebc92a1fb9ec1b7b2cff1c87cb4322819ade0ff56cb25d` |
| `tests/test_gateq38_route_controller.py` | 76,857 | `dc54bd8c20aeb10758296dc42cd7f689f44e6e46144b6cdbd2220e291147b7cc` |
| `tests/test_gateq38_stage_package.py` | 24,333 | `8b6dc421fee44a64e4faa780a48ebd0f7f57af2e0cba7e818f5ed16035a78da1` |

These are SHA-256 digests of the exact blobs in source commit `50dd0a3`.

## Explicitly not proved

This checkpoint does not prove native Linux execution of the package validator,
privileged extraction or protection of the runtime tree, qualification-user write
denial, packaged `edge-acquire --help` or Qwen3.8 execution, instance-generation-bound
status/evidence transport, any cloud create, the complete 64-block route, stock
parity, same-session recovery, packaged cold acquisition/cache reuse, or RTX 30/40/50
qualification. It used no provider resource and no model or archive bytes (USD 0).

The next unblocked no-spend step is the privileged Qwen3.8 Linux host-runtime and
protected instance-generation-bound status/evidence bridge. Paid route actions remain
blocked until that gate, an exact checked-in reservation, and fresh capacity/pricing
evidence satisfy the remaining USD 44 ceiling.
