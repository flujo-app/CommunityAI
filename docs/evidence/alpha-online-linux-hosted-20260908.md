# Hosted Linux online installer — scoped acceptance

**Passed for actual HTTPS download, protected-copy verification, APT installation
and removal.** A separate log/state audit supplies the accepted evidence. The
original diagnostic harness failed after the production installer returned zero
because its `/proc/exe` observer captured no APT arguments. That failure remains
unchanged; its planned post-online CPU check did not run. Native validation is
referenced from the [byte-identical offline package acceptance](alpha-normalized-linux-20260908.md).
The [machine-readable record](alpha-online-linux-hosted-20260908.json) preserves
these distinctions and hashes the raw records.

| Item | Exact identity or result |
| --- | --- |
| Production script | `communityai-0.1.0-alpha.20260908.1-linux-online.py`, 13,662 bytes |
| Script SHA-256 | `59a00906d358c1cac0046e9c323a612e7f6fdb824d21ba562de0bae18ba39b0b` |
| Downloaded package | `communityai_0.1.0~alpha.20260908.1_amd64.deb`, 2,302,428,788 bytes |
| Package SHA-256 | `714b9a7ac9121f3cf3b85f9677d541b488c2f00020bc6e1ce73ba7081f851576` |
| Production script result | Exit 0 after 1,297.439 seconds |
| Ordinary user | UID 1000, Ubuntu 22.04 system Python 3.10 |
| Container limit | Two CPUs, 3 GiB memory; release/source mounts read-only |

The unmodified production script fetched the complete package from its embedded
[public Cloudflare R2 URL](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai_0.1.0~alpha.20260908.1_amd64.deb).
The actual output records 100% of the pinned byte count and successful SHA-256
verification. It then used real `sudo` and the embedded protected-copy helper.
The passwordless sudo rule applied only inside this disposable acceptance container.

The observer independently hashed the protected package to the same SHA-256 and
recorded root ownership, directory mode `0755`, and file mode `0644`. APT's own
`Get:78` output at wrapper-log line 160 names that exact protected package path
and version. This supplies the input binding that the process observer missed.
APT downloaded 24.0 MB of official Ubuntu packages for the declared
`gnome-keyring` recommendation and its dependencies, then installed CommunityAI.
The protected-helper verification message is buffered in the log; its printed
position is not used as a timing measurement.

The actual `dpkg.log` transaction records installation beginning at
2026-09-09 01:13:41 UTC, the exact version installed at 01:16:02, removal at
01:16:04, and `not-installed` at 01:16:05. The final read-only audit independently
confirmed no package entry, no `/opt/communityai`, no app or package-manager
process, no unreadable process-name entry, no online/protected staging directory,
and an empty `dpkg --audit`. The external user-state sentinel hash was preserved.
The shared `/opt` parent remained because the image also contains tooling there.
The container network was disconnected after the audit.

No download or installation was repeated to resolve the observer failure. The
first read-only audit also retained a failed expectation that the final removed
package event would still include a version; `dpkg` correctly records `<none>`.
The corrected audit validates the actual installed/removal version events and
the final absent state.

The earlier offline evidence already records CPU/CUDA native checks for this
exact package hash. No post-online native, GUI, model, public-peer, credential-store,
or public-canary check is claimed here. This does not replace full Gate 15 worker
and settings lifecycle evidence or establish a new Debian 12 or bare-metal replay.
Publisher signing remains deferred until after alpha.
