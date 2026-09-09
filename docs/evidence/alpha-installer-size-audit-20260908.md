# Qualified alpha installer size audit — September 8, 2026

The qualified installers bundle the Python inference engine, PyTorch 2.6.0+cu124,
NVIDIA runtime libraries and Hivemind. Model weights are downloaded separately.
The GPU engine dominates both payloads. Linux also contains substantial duplicate
regular-file data that is a concrete packaging optimization candidate.

All GB/MB figures below use decimal units. Component rows are subsets of the
unpacked payload; duplicate bytes overlap those components.

| Measurement | Windows | Linux |
| --- | ---: | ---: |
| Installer download | 2,519,046,440 B | 3,781,591,484 B |
| Unpacked regular files | 4,487,021,701 B | 8,591,203,661 B |
| PyTorch and NVIDIA/CUDA libraries | 3,804,955,387 B | 7,801,051,434 B |
| bitsandbytes | 248,477,193 B | 250,434,156 B |
| Complete desktop portion outside the node | 122,461,606 B | 200,833,265 B |
| Hivemind directory including p2pd | 43,339,835 B | 10,346,393 B |
| Redundant bytes by identical stored SHA-256 and size | 23,211,174 B | 3,260,409,997 B |

Windows' CUDA libraries reside inside the torch directory. Linux's total combines
the separately classified NVIDIA and PyTorch files, including their duplicate
copies. Other Python libraries and the node executable account for the remainder.
Some Python code is also frozen into the executables, so directory attribution
is not a full module-by-module executable analysis.

Linux's 62 duplicate hash groups represent 37.951% of its unpacked regular-file
bytes. For example, `libtorch_cuda.so` is 902,652,937 bytes at both
`node/_internal/libtorch_cuda.so` and `node/_internal/torch/lib/libtorch_cuda.so`.
`libtorch_cpu.so` and large NVIDIA libraries are duplicated similarly. The
inventory records zero symlinks, and its use of `lstat` distinguishes actual file
copies from links. Original build output already reports the same 8.591 GB and
4,930 files immediately after PyInstaller collection. The duplication therefore
predates Debian staging, which preserves links; it is not caused by `.deb`
compression or by a later copy onto Windows.

Windows has 4,944 regular files and only 0.52% redundant content, mostly shared
Python/OpenSSL support. The Windows GUI is about 122 MB, while the node runtime
is 4.365 GB. The signed bootstrap/catalog is about 17 KB. There is no second
large PyTorch copy in the GUI.

Both platforms include ten bitsandbytes CUDA variants. On Windows, nine variants
other than the bundled PyTorch cu124 profile occupy 223,167,488 bytes unpacked
and approximately 67.6 MB in the existing ZIP. Compatibility with the supported
loader/profile must be checked before pruning these. ZIP compression attribution
does not establish the saving in the solid Inno installer.

The packaged files include GPU runtime libraries such as cuBLAS/cuDNN and runtime
compilation libraries, not a complete CUDA development toolkit or an NVIDIA
driver installer. No conventional model-weight files were found. The Debian
archive consists almost entirely of compressed runtime payload: its control
archive is only 2,004 bytes. Current compression settings are Inno
`lzma2/fast` with solid compression and Debian XZ level 1; stronger compression
has not been measured on these candidates.

Recommended next packaging work is to preserve all required loader paths while
eliminating duplicate Linux library data, validate CUDA-variant pruning, and then
measure the rebuilt installers. Keeping one physical copy of every identical
Linux file would leave 5,330,793,664 bytes of unique content; that is an accounting
bound, not a validated replacement bundle or a predicted compressed download.
Removing libraries merely because a model does not explicitly call them may
break stock PyTorch's loading dependencies. A substantially smaller base installer
could instead make the GPU runtime a separate verified download; GPU users would
still download those libraries when enabling that capability.

## Evidence and limits

This is an inspection of the qualified Windows `76b6d84` and Linux `bf67f0d`
payloads recorded in [the artifact inventory](alpha-artifact-audit-20260907.json).
No installer or runtime was modified, repacked, executed or requalified.

Windows filesystem sizes were compared with every entry in the qualified
provenance and ZIP central directory: no missing, extra or size-mismatched files.
Linux used the exact provenance accompanying the `.4` Debian candidate, with
its digest checked against the installer sidecar. Only 21,636 bytes of Debian
headers, XZ index and small control metadata were read, establishing the
8,595,763,200-byte uncompressed tar size. Large binaries were not rehashed or
decompressed. Duplicate groups use their existing recorded SHA-256 identities.
Docker and WSL remained off; no model or desktop window launched.

The detailed private reports are retained at
`.gate13-runs/installer-size-audit-20260908/windows.json` and `linux.json`.
The Windows metadata audit helper is retained beside them. Exact new installer
savings remain unmeasured and require packaging and frozen-runtime validation.
