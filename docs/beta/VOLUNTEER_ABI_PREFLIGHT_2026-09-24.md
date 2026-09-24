# Volunteer package ABI preflight

Status, 2026-09-24: **Ubuntu 20.04 compatibility failed** for the first frozen
engineering artifact. This artifact was neither offered to the volunteer nor
published. The full beta remains open.

The isolated Linux build from beta commit `e1e0fcd15534c123cd0b601e4bfbcedce507c5de`
completed its packaged desktop, node and worker diagnostics, and a fresh process
verified its provenance, catalog input and checksums. Its archive was
3,041,298,327 bytes, SHA-256
`537d55ffade9b680faf28b3eab888f87b9fd8b296d3a2c748327f19b7bbb7f70`.
The copied archive was hashed again. The local artifact and details are under
`.communityai-beta/volunteer-build-e1e0fcd/`. It is unsigned, uninstalled,
credit-disabled and not a shareable volunteer package.

The volunteer reports Ubuntu 20.04. Its Focal libc6 line is glibc 2.31
([Ubuntu's Focal package record](https://bugs.launchpad.net/ubuntu/focal/amd64/libc6-dev)).
A read-only `readelf --version-info` scan of the packaged regular ELF files
found 67 requiring newer GLIBC symbols, up to `GLIBC_2.36` in bundled
`libstdc++.so.6` and `libexpat.so.1`. The earlier exploratory scan counted 75
including symlink aliases. Therefore the package cannot be used on the reported
host as built. The container used Debian glibc 2.36, not an Ubuntu 20.04 ABI.

`desktop/linux_abi.py` now checks every regular packaged ELF against 2.31 with
a 60-second scan deadline. The volunteer builder invokes it after runtime
normalization, before smoke checks and archive compression, and also during
fresh-process release verification. A missing `readelf`, unreadable ELF,
timeout or newer symbol fails closed. The three synthetic unit cases passed in
0.014 seconds; the actual incompatible bundle was refused in under three
seconds by the integrated scanner. This is a necessary binary compatibility
check, not an installed Ubuntu or GPU inference test.

Next, produce a package using a runtime and all native libraries compatible
with Ubuntu 20.04, rerun this ABI gate, and qualify the installer/anchor service
on that OS. Then exercise the full eight-card Sharing workflow and actual H100
inference under the volunteer's limits. The current build does not qualify the
requested DeepSeek, GLM, vLLM, credits or full beta.

The Ubuntu 20.04 build probe also showed that its headers predate the cgroup
fields of `clone3` and omit the `close_range` syscall number. The native
extension now declares the fixed 88-byte cgroup `clone3` argument layout and
uses x86-64 syscall 436 when the headers lack it. These values match the
[Linux kernel x86-64 syscall table](https://github.com/torvalds/linux/blob/master/arch/x86/entry/syscalls/syscall_64.tbl)
and [kernel UAPI definitions](https://github.com/torvalds/linux/blob/master/include/uapi/linux/sched.h).
The extension compiled in the Ubuntu 20.04 container in under two seconds.
Compilation does not establish that the volunteer kernel enables these syscalls
or that its cgroup v2 hierarchy is delegated; those remain installed-host gates.
