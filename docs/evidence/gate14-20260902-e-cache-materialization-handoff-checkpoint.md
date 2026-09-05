# Gate 14 cache-materialization handoff checkpoint

Recorded: 2026-09-02 (America/Bogota)

Status: implementation checkpoint only. Gate 14 remains `IN PROGRESS`. This
document is not fresh-cache, packaged-execution, or hardware acceptance evidence
and does not authorize a paid run.

## Result

Gate 14 now has a fail-closed, two-phase official-source cache boundary for the
exact Windows/Qwen and Linux/Gemma profiles. The implementation was developed on
top of source `f686016b8c41d7756682deabee5967d661be5c73`.

The ordinary materialization phase can read a controller-owned canonical plan
and lifecycle template, download only the exact manifest-selected files from
the official source, verify the exact cache tree, and leave a canonical
record/binding/handoff below its writable work root. It cannot write the
controller staging root. A separate privileged promotion phase revalidates the
plan, source files, template, record, binding, handoff, and physical cache
before creating the protected lifecycle record and configuration.

No GCP, Fly.io, GitHub provider, reservation, or cloud resource was created or
changed. No production model artifact was downloaded. This checkpoint spent
USD 0 under the combined USD 100 ceiling.

## Implemented boundary

- The protected plan binds the platform, exact manifest filename, source commit,
  disjoint absolute work/staging roots, lifecycle-template digest, and normalized
  SHA-256 of the materializer, lifecycle, acquisition, and manifest sources.
- Template canonical form, digest, platform/model/manifest/source identity, and
  exact roots are verified before any transport check, cache creation, or
  acquisition call. A predictably unpromotable input therefore cannot trigger
  the multi-gigabyte transfer.
- Official transport rejects Hugging Face endpoint overrides, HTTP/HTTPS/all
  proxies from both environment and system proxy discovery, Requests/cURL CA
  overrides, and `SSL_CERT_FILE`/`SSL_CERT_DIR`. Acquisition uses no token,
  requires direct upstream, and permits at most three resumptions.
- The cache verifier accepts only the exact manifest-artifacts tree. It rejects
  symlinks/reparse points, special or extra entries, case collisions, changed
  directory inventories, wrong sizes/digests, and incomplete selections.
  Windows uses no-follow share-read-only native handles; POSIX uses
  `O_NOFOLLOW` handles. Open-handle and path identities are rechecked before
  release.
- The materialization record must prove an empty cold cache, complete
  direct-upstream/no-mirror transfer, exact repository/revision/dtype,
  privacy-safe output, and the target runtime platform. Its lifecycle binding
  now carries the source commit, plan digest, and complete materializer-source
  digest.
- Promotion structurally validates root/SYSTEM-or-Administrators ownership and
  protected modes/DACLs without imposing the qualification token's write-denial
  rule on the controller itself. Normal lifecycle loading and every action
  boundary retain the stricter proof that the qualification process cannot
  mutate staging.
- Newly promoted Windows files receive an explicit protected DACL owned by
  Administrators and granting full access only to SYSTEM and Administrators
  before structural validation. POSIX promoted files must be root-owned and
  mode `0600`.
- Verified staged record/config creation is an explicit commit point.
  Interrupted source-handoff deletion preserves those protected outputs; a
  retry revalidates the staged files and exact cache, then idempotently removes
  whatever handoff members remain. Protection or pre-commit write failure
  removes staged partials while leaving the complete work handoff retryable.
- Cleanup and no-clobber writes use bounded retries and return only generic
  errors/results without private paths, URLs, credentials, or response bodies.

## Source and test identities

Final working-tree SHA-256 values:

- Cache materializer:
  `e6066464fc7aecc78a8df18fef8ce5919b82fe19e6922737e2b29b824f60172e`
- Packaged lifecycle:
  `a62469ea1ca1a764606add76b4e4913284d260014d6273cc9650ee722f0719bb`
- Cache-materializer tests:
  `e4cc177c456cb1f234fb9c81e1a9584cb5fade0cbdedf36b8b0e8f562e1c1fa4`
- Packaged-lifecycle tests:
  `9084f7f94d0f2336ab9e6b1e2812423072c71ea4efaac4b33c3d17e269a6beb2`

The controller plan records fresh normalized source digests at creation time and
both phases recompute them before accepting or promoting a handoff. Those plan
values, not this narrative, are the runtime trust input.

## Verification

Using the repository's existing environments:

```text
.venv-cuda/Scripts/python.exe -m pytest \
  tests/test_gate14_cache_materializer.py \
  tests/test_gate14_packaged_lifecycle.py -q
# 96 passed

.venv-cuda/Scripts/python.exe -m pytest tests/test_gate14_*.py -q
# PowerShell-expanded file list: 253 passed
```

Black, isort, Python compilation, and scoped `git diff --check` passed. An
independent adversarial review reproduced the 96-test focus and 253-test full
matrix. It returned PASS after verifying privileged default promotion, explicit
Windows output protection, rollback before the commit point, retry after partial
handoff cleanup, and template rejection before acquisition.

## Deliberately incomplete

- Neither exact production profile has been freshly materialized from the
  official source on its native clean host. The Windows/Qwen and Linux/Gemma
  selected caches total 14,850,015,469 bytes across the two sequential runs.
- The GCP host bootstrap/controller does not yet create the protected plan and
  lifecycle template or invoke the ordinary materializer and privileged
  promoter in the required identities. That integration must be source-bound
  and tested before provisioning.
- No retained production archive has passed this new boundary on a fresh host,
  no hardware calibration was attempted, and no Gate 14 acceptance pass is
  claimed.
- Native authentication, inventory, quota, pricing, and the current-epoch
  ledger must be revalidated immediately before any future bounded reservation.
- Gate 14 remains `IN PROGRESS`; Gate 15 remains waiting.

## Next unblocked gate

Wire controller-owned plan/template creation and the two materialization phases
into the exact Windows/Linux host bootstrap, then exercise the no-download
preflight and native cleanup paths. Do not create paid hosts until that
source-bound integration is runnable. Afterward, revalidate provider and USD
100 boundaries, record a bounded reservation, run Windows then Linux
sequentially, and prove exact cleanup.

Do not work on credits or macOS.
