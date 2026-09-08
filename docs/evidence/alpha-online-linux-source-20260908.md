# Focused Linux online-installer and archive validation — 2026-09-08

**49 tests and 54 subtests passed in 2.29 seconds**, with no failures or skips,
in the cached Ubuntu 22.04 builder image. The [companion record](alpha-online-linux-source-20260908.json)
binds the source and retained logs. This is source validation; no release runtime
was built or published.

The run covered `tests/test_linux_online_installer.py`,
`tests/test_runtime_archive_hardlinks.py` and
`desktop/tests/test_runtime_packaging.py`. It exercised the Linux-only
`O_NOFOLLOW` source-symlink rejection, protected-copy byte verification, download
and package-manager failure fixtures, actual inode hardlinks, archive writing
and both extraction consumers. Unsafe link targets, metadata mismatches and
cross-inventory links were rejected. The Debian staging fixture preserved
hardlinks through a simulated cross-device copy fallback.

The disposable container had two CPUs, 2 GiB memory, no network or GPU devices,
read-only repository/environment mounts and a 128 MiB temporary filesystem. It
ran as UID 1000 with Qt configured offscreen. All owned containers were removed.
There were no APT commands, dependency installations, model loads, GUI launches
or full builds.

The first attempt stopped before test collection because the cached venv's
Python symlink targeted a binary absent from this image. Read-only inspection
found the image's matching Python 3.12.14; the successful run used it with the
existing cached site-packages and pytest 9.1.1. No environment files changed.
The failed start and inspection logs remain separately hashed.

The protected-copy tests emulate the root identity and keep APT inert. Actual
sudo, operating-system enforcement of root ownership, a real HTTPS package
download and the complete APT handoff remain acceptance work. Library payloads
are tiny fixtures, so this result establishes neither a new compressed package
size nor frozen GPU/runtime behavior. Previous Windows fixture records remain
unchanged.
