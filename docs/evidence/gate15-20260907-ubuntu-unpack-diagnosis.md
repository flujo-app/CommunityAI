# Ubuntu package-unpack timeout diagnosis

The first Ubuntu 22.04 lifecycle attempt exceeded its **540-second package
installation deadline**, before launching the installed GUI. This is a failed
qualification attempt, not evidence that installation completed. The same `.4`
package has separate passing Debian 12 lifecycle evidence. This note investigates
the difference without running another installation or changing package bytes.

**September 8 follow-up:** the unchanged `.4` package subsequently passed the
complete Ubuntu lifecycle with a two-core/6 GiB resource cap and a longer finite
operation deadline. Initial installation took 329.971 seconds. The earlier
failure remains recorded; its cause is unconfirmed. The diagnostic proposals
below are retained as history. [Passing retry](gate15-20260908-frozen-ubuntu-installer.md).

## Verified package facts

`communityai_0.1.0~alpha.20260907.4_amd64.deb` is 3,781,591,484 bytes, SHA-256
`a2cc0548cd51f98ed7a9c208be18b53a701a9317cbc63293d4bf7d1e14151517`.
The builder invokes `dpkg-deb --root-owner-group -Zxz -z1 --build`. Its staging
hardlinks avoid a build-time copy; they do not make installation free of file
extraction. The `.4` control-only repack preserved the compressed runtime payload
exactly. [Debian package provenance](gate15-20260907-frozen-debian-installer.json).

A bounded read of the host archive's headers and XZ index found:

| Observation | Value |
| --- | ---: |
| `data.tar.xz` compressed bytes | 3,781,589,288 |
| Uncompressed tar bytes | 8,595,763,200 |
| XZ blocks | 2,733 |
| XZ index bytes | 19,432 |
| Control members | `.`, `./control`, `./preinst`, `./prerm` |

There is no packaged `md5sums` control file. This matches the builder source and
its retained `dpkg-deb --info` output. Debian documents that dpkg generates this
information during unpacking when the package does not supply it. MD5 here is
package-integrity metadata, not the release's security boundary; the existing
SHA-256 provenance verification remains necessary.
[Debian `deb-md5sums(5)`](https://manpages.debian.org/bookworm/dpkg-dev/deb-md5sums.5.en.html).

## Relevant version differences, not a proven root cause

Upstream dpkg **1.21.13** added multithreaded XZ decompression, requiring liblzma
5.4.0 or newer, under Debian issue 956452. Ubuntu Jammy's 1.21.1 line predates
that change; Debian 12's 1.21.22 follows it. This archive has thousands of XZ
blocks, so it contains work that a parallel decoder can distribute. Dpkg
1.21.10 also switched its MD5 implementation fully to libmd. The changelog does
not establish an MD5 speed regression or fix for this particular package.
[Upstream dpkg changelog](https://launchpad.net/debian/+source/dpkg/+changelog),
[Ubuntu Jammy package version](https://manpages.ubuntu.com/manpages/jammy/man1/dpkg-deb.1.html).

The reported CPU activity and continuing partial extraction are consistent with
a slow unpack stage. They do not distinguish decoder work, digest calculation,
filesystem synchronization, or host/container overhead. No comparative CPU
profile or controlled timing was captured by this audit. Do not label the
timeout a confirmed dpkg MD5 defect.

## Next bounded qualification

1. Keep the failed attempt and its cleanup record. Begin the next attempt with
   adequate host space, a fresh owned Ubuntu environment and no concurrent large
   hashing/build work. Record exact dpkg/liblzma versions, filesystem/mount,
   cgroup CPU limits and the verified installer SHA-256.
2. Time installation separately from the GUI lifecycle. Capture per-process CPU
   and read/write counters for dpkg and its decoder children, plus extracted
   byte progress and a bounded syscall summary if needed. Give the diagnostic
   install an explicit longer deadline; 540 seconds has already proved
   insufficient here. Retain the same full package/provenance verification.
3. If XZ decoding dominates, prepare a separately versioned **zstd-compressed**
   package from the same verified runtime. Jammy supports zstd packages and
   Debian 12 does too. Verify the complete decoded file inventory, permissions,
   symlinks and new package hash, then rerun both installed lifecycles. This
   changes packaging and requires new evidence; it does not require weakening
   any integrity check. [Jammy compression support](https://manpages.ubuntu.com/manpages/jammy/man1/dpkg-deb.1.html),
   [Debian 12 compression support](https://manpages.debian.org/bookworm/dpkg/dpkg-deb.1.en.html).
4. Add a complete deterministic `DEBIAN/md5sums` inventory during packaging as a
   separate candidate improvement, then measure its effect. Do not assume that
   it alone resolves the timeout. Keep the strong SHA-256 release inventory and
   post-install checks.

Do not substitute raw tar extraction for `dpkg -i`, skip verification, disable
normal filesystem safety, or upgrade Ubuntu's package manager solely to obtain a
passing acceptance result. Those would change the tested product or baseline.
No installer, runtime, operating-system package or cloud resource was changed
by this diagnosis.
