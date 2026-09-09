# Engineering installer checkpoint

Date: September 7, 2026. Parent source `b09aa2d67b2789f9fd52c91abdea2b9340bb2fe4`
passed Tests, Style and Production desktop, including both Windows/Linux builds
with the new block grid and download reporting. The changes described below are
the installer slice following that parent. Both complete production installer
builds passed at `0b875c19efd952342671371ddacebc09ca3a774c` in
[CI run 34144852840](https://github.com/flujo-app/CommunityAI/actions/runs/34144852840).

## Implemented

- Inno Setup per-user install, stable application identity, Start menu entries,
  silent install/removal, optional setup/uninstaller signing hook and output hash.
  Unsigned engineering builds must be selected explicitly. Unmarked existing
  applications are refused; obsolete runtime directories are removed only from
  the marked installation.
- Same-user `--prepare-update` IPC requests desktop exit, waits for owned-node
  cleanup, and acknowledges only before releasing the instance endpoint/lock.
  A failed stop retains the process reference and returns failure. A broken Qt
  import returns an error code rather than a windowed traceback dialog.
- Debian builder installs the application, command wrapper and menu entry.
  `preinst`/`prerm` select processes from the marked installation and their
  observed descendants, signal them using PID handles/start-time identities,
  and refuse replacement if any remain. No home-directory state is removed.
- CI builds installers only after the existing release archive/provenance and
  runtime checks. Linux build baseline becomes Ubuntu 22.04 (glibc 2.35);
  a complete fresh build must confirm the target distribution compatibility.

## Observed probes

Windows 10, Python 3.12.9, PySide6, PyInstaller 6.22.2, Inno Setup 6.7.3:

- Native Qt shutdown test used separate application/helper processes and a
  cleanup marker; acknowledgement arrived only after the cleanup callback.
- A real Inno setup installed into an isolated task directory with a distinct
  AppId. Re-running it while a source Qt window served the fake-node fixture
  requested shutdown, observed normal exit, removed an obsolete application
  DLL fixture and preserved external settings/cache sentinels. Silent uninstall
  removed the application and retained those sentinels. Probe registration was
  removed; no production settings or credentials were provisioned or changed.
- The packaged probe contains the actual frozen GUI and a copied GUI executable
  standing in for the node filename. It does **not** run the model/node runtime.
  The first locally built probe failed Qt loading because direct PyInstaller
  invocation picked up a foreign ICU DLL. Rebuilding through the production
  builder's existing restricted-PATH helper resolved it; the upgrade then passed.
  That failure motivated explicit non-dialog error handling for maintenance.

Docker, Python 3.12 on Debian bookworm:

- Copied an executable into a marked temporary installation, started it and an
  unrelated process, stopped the former and preserved the latter.
- A separate process-tree test used a copied shell executable plus its sleep
  child; both disappeared from the executable snapshot while unrelated sleep and
  external cache bytes remained. An unmarked directory was refused.
- Built and inspected an actual `.deb` from executable fixtures. Root-owned
  directories are 0755, launchers/maintenance scripts executable, dependencies and
  control records readable. Cross-filesystem copy fallback was exercised before
  staging was moved beside the source bundle. Containers were removed after use.

The final Windows desktop suite passed 101 tests with two Linux-only skips;
the 12 maintenance/lifecycle tests and both Linux ownership tests also passed
in their respective environments. Root Black/isort and diff checks passed.

## APT signing follow-up

The repository builder generates a new immutable snapshot directory, validates
CommunityAI/amd64 package identity, signs release metadata with a supplied full
GPG fingerprint, exports only the public key and verifies both signatures using
`gpgv`. A Debian container generated a disposable signing key, accepted the
result through scoped `Signed-By`, selected the fixture package, downloaded it
with matching SHA-256, then rejected tampered signed metadata with `BADSIG`.
The container/private key were removed. No production key or repository was
created. GPG repository signing requires no paid certificate.

CI at `0b875c1` passed style and Linux tests; its macOS standalone image verifier
hit the pre-existing ten-second subprocess timeout. That test imports the model
runtime from a fresh Python process. The timeout is raised to 30 seconds while
retaining the exact verification assertions. Tests and Style then passed at
`40de496cafd2bca9725c1a9a8a04e60dc6fcf199`, including the macOS test.
Both full Windows/Linux installers also passed at that revision in
[CI run 34145484606](https://github.com/flujo-app/CommunityAI/actions/runs/34145484606).

## Full Windows runtime follow-up

A clean detached checkout at `0b875c1` produced and verified the complete GUI/node
bundle: 4,486,446,226 bytes in 4,942 files. The archive is 2,693,786,190 bytes,
SHA-256 `c5d59f4ae8c057315cb50fb0dadc211e36ec3ea58e2952c4493f9e39c4ce29fb`.
The explicitly unsigned Inno setup is 2,518,829,949 bytes, SHA-256
`95b2d70382ed91b61079581e3fed0c3b12364ebe70576a25eee3231460f8e4d8`.
[Sanitized evidence](qwen-windows-installers-20260907.json) binds the source,
packages, runtime, probe scripts and observed outcomes.

On Windows 10 with an RTX 2070 SUPER, the source desktop controller drove the
frozen node and a real automatically assigned Qwen3.8 contribution block. Changes
to 20% VRAM/50% processing and 25%/100% stopped the old trees, persisted both
settings and restarted ready workers with the expected allocator ceiling and
processing argument. All 384,054,157 selected artifact bytes were hash-verified;
local Qwen inference continued. Pause removed the worker tree in 0.125 seconds;
restart and the final Pause/cleanup passed.

The first attempt failed a probe assertion that incorrectly multiplied the
already percentage-limited `vram_pool_bytes` a second time. Correcting the probe
to compare against physical GPU capacity resolved that assertion; no product
enforcement change was needed. This preserves the failed attempt as a harness
error, rather than counting it as a passing product run.

The complete setup then installed into an isolated directory. Source Qt using
the production `NodeLifecycleSupervisor` started the installed frozen node and
a ready Qwen3.8 worker for block `3:4`. Re-running setup requested shutdown and
removed all seven recorded node/worker/transport processes before replacement.
The settings hash and external cache sentinel were unchanged. Silent uninstall
removed the application and preserved external state. Owned processes, test
native credential, installed executable and installer registration were separately
verified absent afterward. No production credential was changed.

The health/download Qt view was also rendered against the live packaged node and
inspected. These checks use source Qt/controller integration with frozen runtime;
they do not claim literal frozen-GUI slider interactions, remote Qwen processing
duty-cycle measurement under load, Linux Qwen lifecycle or a broader GPU profile.

## Remaining release outcomes

These are bounded engineering results, including the real Windows runtime case
above. Complete Windows/Linux Qwen resource controls under load,
fully frozen desktop lifecycle, Linux real-worker upgrades/removal, login-entry cleanup,
explicit cache deletion policy and supported-distribution measurements remain.
SignPath's CUDA/upstream eligibility, trusted signing, Store account/certification,
release maintainer address and signed HTTPS APT distribution remain unresolved.
An explicitly authorized free-program eligibility inquiry was sent to SignPath
and confirmed in Gmail Sent on September 7; no enrollment, signing approval,
Store submission, public installer release or cloud host was created.
