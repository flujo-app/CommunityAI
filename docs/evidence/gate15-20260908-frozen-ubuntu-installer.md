# Ubuntu 22.04 frozen installer lifecycle

**Passed**, using the unchanged qualified
`communityai_0.1.0~alpha.20260907.4_amd64.deb`, SHA-256
`a2cc0548cd51f98ed7a9c208be18b53a701a9317cbc63293d4bf7d1e14151517`.
The [machine-readable evidence](gate15-20260908-frozen-ubuntu-installer.json)
binds the runtime, installer source, harness and raw observations.

The disposable Ubuntu 22.04 environment ran locally through WSL2/Docker with
two CPU cores, a 6 GiB memory ceiling, no container swap, CUDA passthrough,
ordinary UID 1000 and a private Xvfb/native-keyring session. Package maintenance
ran as root with `SYS_PTRACE` for cross-user process inspection. Each package
operation had an 1,800-second deadline; the container had a four-hour backstop.
Older duplicate Windows build directories were moved to a separate local drive
before installation, preserving their archives and the qualified candidates.

| Package operation | Result | Seconds |
| --- | --- | ---: |
| Initial installation | Passed | 329.971 |
| Replacement while the installed app and sharing worker were active | Passed | 390.383 |
| Removal while active | Passed | 31.038 |
| Reinstallation with retained state | Passed | 349.084 |
| Final removal while active | Passed | 30.988 |

Each of the three actual installed GUI/node launches generated three local
Qwen3.5-0.8B tokens and verified 384,054,157 bytes for the Qwen3.8 sharing block
`60:61`. Each owned runtime tree had 11 observed processes. Replacement and
removal stopped those trees and preserved config bytes, the native credential
and a retained-data sentinel. The real cached model files stayed outside the
installation directory. This used existing caches and a private loopback DHT.

An additional ordinary-user check created and removed the actual XDG login
entry using production startup functions and the installed executable command.
The [frozen Linux checkbox replay](gate15-20260908-frozen-linux-login.md) separately
exercises the literal Qt control through accessibility.

After the lifecycle, a fresh independent audit found no installed executable,
installed package, frozen runtime, DHT process or test credential. The retained
sentinel survived. The exact disposable container was then stopped and removed;
absence was independently checked. The qualification volume, model caches and
raw evidence remain available.

The earlier [540-second unpack timeout](gate15-20260908-ubuntu-install-timeout.json)
remains a separate failed attempt. The retry completed initial installation in
approximately five and a half minutes with the same package bytes. Resource
and host-load differences prevent assigning a proven cause to the earlier
timeout; no package-manager or compression change was needed for this pass.

This qualifies Ubuntu 22.04/Xvfb with the tested NVIDIA device and same-version
replacement. Physical Wayland desktops, Ubuntu 24.04, broader hardware and a
different-version Linux upgrade retain their separate scope. The Windows
different-version upgrade has its own passing evidence. Signing remains
owner-deferred after alpha.
