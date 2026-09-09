# Normalized Linux installer acceptance — 2026-09-08

**Passed for the scope below.** The fresh Linux release was built from clean commit
`84205f93fc73d3babd39e238944b97fab0d11b3e` (tree
`5bd44e4afb6b6911f5ddd1bb3d492dad438883e8`). The source checkout, output and build
directories were new; the previous qualified artifacts were preserved. The
[machine-readable record](alpha-normalized-linux-20260908.json) binds the source,
raw logs, normalization metrics, frozen checks and installed-file verification.

| Artifact or measure | Actual result |
| --- | --- |
| Installer | `communityai_0.1.0~alpha.20260908.1_amd64.deb` |
| Installer bytes | 2,302,428,788 |
| Installer SHA-256 | `714b9a7ac9121f3cf3b85f9677d541b488c2f00020bc6e1ce73ba7081f851576` |
| Previous qualified `.4` installer | 3,781,591,484 bytes |
| Download reduction | 1,479,162,696 bytes, approximately 39.1% |
| Regular-file payload | 5,161,115,250 bytes across 4,900 files |
| Internal symlinks | 35; all preserved by the actual Debian installation |
| Debian Installed-Size | 5,040,152 KiB, including the installation marker and rounding |
| Runtime archive | 3,017,723,439 bytes; SHA-256 `28f24f55c2e53c44d9235dbc150223e9bbc7b532945dc72ff28298cd82c55c79` |

The node normalization removed nine unused bitsandbytes CUDA variants, saving
224,905,160 bytes and retaining CUDA 12.4. Its regular payload decreased from
5,185,185,572 to 4,960,280,412 bytes. This fresh freeze already contained internal
library symlinks and required zero additional hardlink replacements. The reported
artifact logical total of 5,528,581,899 bytes includes 367,466,649 bytes counted
again through symlink targets; it is not the unique installed payload size. The
earlier hardlink-savings estimate was an inventory estimate for the older artifact.

The full build used the audited cached Ubuntu 22.04 image
`sha256:0042575ad9d57e21900044ed3227ca15bb5017b033d84781d6a57f16e9db51a3`,
two CPUs, 6 GiB memory, no network and offscreen Qt. The existing dependency
environment remained read-only. The build's bundled startup/UI contract checks
and a separate release/archive/provenance verification passed. A separate
read-only, network-isolated container with two CPUs and 3 GiB then passed the
frozen CPU matrix check, required-CUDA matrix and linalg checks, actual retained
`libbitsandbytes_cuda124.so` NF4 roundtrip, and server entry-point contract.

The new installer built in 514.229 seconds. In a fresh Ubuntu 22.04 acceptance
container, official `python3` and `sudo` dependencies were installed without
recommends before disconnecting its network. The exact `.deb` installed in
132.119 seconds. Every regular file's SHA-256 and executable mode matched the
verified bundle, and every symlink resolved to the recorded internal target.
As ordinary UID 1000, the installed CPU, CUDA and server checks passed in 3.265,
3.902 and 7.355 seconds respectively. The CUDA NF4 maximum absolute error was
0.14501953125. No weights were loaded and no network was joined.

Removal passed in 0.767 seconds. An external user-state sentinel survived. A
separate post-removal observation found no installed runtime, package entry, app
or package-manager process, or `dpkg --audit` problem. The credential store was
untouched. The finite native-check container was removed; the acceptance
container was retained idle and disconnected for a separate online acceptance
run. Its passwordless sudo fixture exists only inside that disposable container.
The single Windows-host installer copy was independently size/hash verified.

This evidence covers the normalized bundle and bounded offline installer/native
acceptance on Ubuntu 22.04 under the recorded WSL2/Docker host. It does not replace
the earlier full Gate 15 GUI/worker lifecycle evidence, claim a new Debian 12 or
bare-metal replay, or establish model generation, periodic catalog draining,
public canary readiness, or the online downloader's sudo/APT handoff. Signing
remains deferred until after alpha. A read-only-mount bytecode-write preflight
failure is retained in the JSON; the corrected read-only AST parse passed, and
no product build/native/install failure occurred in this replay.
