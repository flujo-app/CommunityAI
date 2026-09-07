# Engineering installer checkpoint

Date: September 7, 2026. Parent source `b09aa2d67b2789f9fd52c91abdea2b9340bb2fe4`
passed Tests, Style and Production desktop, including both Windows/Linux builds
with the new block grid and download reporting. The changes described below are
the installer slice following that parent; its final production builds are pending.

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

## Remaining release outcomes

These are bounded engineering/fixture results, not fresh full-runtime installer
or Qwen-sharing acceptance. Complete Windows/Linux Qwen resource controls,
worker-tree shutdown during real upgrades, reinstall/removal, login-entry cleanup,
explicit cache deletion policy and supported-distribution measurements remain.
SignPath's CUDA/upstream eligibility, trusted signing, Store account/certification,
maintainer email and signed HTTPS APT distribution remain unresolved. No signing
request, Store submission, public installer release or cloud host was created.
