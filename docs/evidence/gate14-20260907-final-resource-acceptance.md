# Gate 14: final Windows/Linux resource acceptance

Date: 2026-09-07. **PASSED for the bounded alpha resource-control scope.**
The Windows complete frozen GUI/node package came from `76b6d84fc52342af4fd2315926b187aaa36b1378`,
source tree `474edae23a04c7be8d55241cab7fdbe8255b215b`. Linux was rebuilt on Ubuntu
22.04 from `bf67f0d68df067f43797b4cd47a98f1fea49b2bc`, tree `436c44cc1aa0ae4f4e05fdefb7bf17caa39d9a5f`.
Application and catalog source files are unchanged between these commits;
Linux packaging fixes declare an X11 dependency and exclude optional Triton JIT.
The earlier CI artifact used a README-only PR merge commit and then failed native
X11/GPU acceptance; it is not substituted for this corrected package. Independent release
verification checked the archive and complete file inventory, modes, source
identity and signed catalog inputs. The [portable record](gate14-20260907-final-resource-acceptance.json)
contains package hashes, every checkpoint and cleanup evidence.

## Acceptance

Both packages passed all 11 actual Qt-control checkpoints: fresh 100%/100%
defaults with sharing off, explicit sharing opt-in, live slider changes, repeated
Pause/Start, retained settings and paused intent after full desktop restart.
The host driver waited for real node/worker observations before acknowledging
each UI step. All selected worker artifact bytes were verified before load.
Local Qwen3.5-0.8B generated three real tokens at every checkpoint.

VRAM limits bounded the worker allocator at 25% and 20%. At 1%, an assignment
that could not fit waited with a clear VRAM reason, no worker PID and no restart
loop, while preserving the user's sharing selection. Raising the budget to 25%
restored sharing. Pause removed the complete observed worker tree. Final GUI/node
trees and the test native credentials were removed on both platforms.

The same packages separately rejected Start under schedule, power, bandwidth and
storage limits, with the other guards open and local inference still usable.
Existing [Windows power recovery evidence](qwen-power-recovery-20260906.json)
also covers automatic resumption after a measured load subsided.

## Real processing load

Each setting ran at least 20 seconds of repeated 128-token prefill through the
same assigned Qwen3.8 block, in one admitted RPC session. Requests rewound the
session before repeating the same input. All outputs were finite and bit-identical
within each platform across settings. Warmup requests were excluded.

| Platform | Processing | Requests | Median request | Mean whole-GPU activity |
| --- | ---: | ---: | ---: | ---: |
| Windows | 100% | 325 | 62.0 ms | 46.3% |
| Windows | 50% | 173 | 94.0 ms | 33.4% |
| Windows | 25% | 77 | 219.0 ms | 23.4% |
| Linux | 100% | 384 | 47.4 ms | 56.5% |
| Linux | 50% | 238 | 78.8 ms | 36.1% |
| Linux | 25% | 86 | 203.6 ms | 25.6% |

Supplementary production-runtime tensor probes measured Linux CPU duty of
95.1/48.3/24.6% and CUDA duty of 96.6/48.8/24.5% at requested 100/50/25%.
The CUDA allocator accepted 32 MiB below a 256 MiB ceiling and rejected 257 MiB.
Earlier [Windows tensor probes](gate14-20260907-resource-sliders.md) independently
exercised the same limiter and allocator.

## Fixes found by acceptance

- An insufficient VRAM budget previously caused repeated worker restarts. A
  distinct memory-budget exit signal now leaves that launch configuration waiting
  until the budget or assignment changes; ordinary crash recovery remains intact.
- Catalog migration previously lost cache/resource preferences when an identical
  manifest moved into the managed directory. Refresh now matches verified manifest
  identity, retains those preferences and adopts the managed path. A different
  manifest digest cannot inherit preferences through a reused model name.
- Minimal Linux installations lacked `libxcb-shape0`, preventing Qt's X11 plugin
  loading. The Debian package now declares it, and CI exercises the frozen UI and
  onboarding through X11/Xvfb in addition to the existing offscreen checks.
- Optional PEFT/bitsandbytes imports initialized Triton's compiler on GPU hosts,
  which failed inside the frozen runtime. The Linux package now excludes this
  optional JIT and retains the approved eager/native kernels; contributors do not
  need a compiler or Python development headers for these profiles.

The [earlier Linux failures and cleanup](gate14-20260907-linux-failed-attempts.json)
remain recorded alongside their retained private logs. The staged Windows
[checkpoint](gate14-20260907-real-qwen-windows.json) is separate from this final
catalog-bearing package acceptance. Catalog migration tests passed on Windows and
Linux (36 each); Linux resource regressions passed (68), and the desktop suite
passed (101, with two Linux-specific skips on Windows). Test/style jobs and both
production package/installer jobs passed for this source. Two CodeQL findings were
reviewed against their complete data-flow paths and dismissed as false positives
with [specific rationale](gate14-20260907-codeql-triage.md); scanning remains enabled.

## Environment and limits

Windows ran as a non-elevated user on Windows 10 Pro 10.0.19045. Linux ran as
ordinary UID 1000 on Debian 12 in a WSL2 Docker container, with X11/Xvfb and a real
Secret Service credential store; its archive was built on Ubuntu 22.04. Both used
the host RTX 2070 SUPER with 8 GiB VRAM. Linux used actual CUDA passthrough and
the frozen desktop/node executables. This does not claim a physical Ubuntu or
Wayland desktop playthrough, another GPU generation or broad hardware support.

Processing is paced sharing compute time. Brief bursts, loading, downloads, local
inference and other applications are outside an instantaneous whole-device cap;
whole-GPU samples include other applications. VRAM bounds cover the contribution
allocator, with driver/context overhead and local inference separate. Power and
bandwidth are sampled pause guards rather than hard power caps or traffic shapers.
This is one-block resource acceptance, not a full-route conversation benchmark.

The block-health grid, reported peer metadata and local client/worker download
progress are included in these packages; their detailed
[display/integrity tests](desktop-health-downloads-20260907.md) remain the evidence
for all display states. Remote download percentages and unreported spare capacity
are not inferred. Installer lifecycle is Gate 15; Windows's fully frozen
[installation result](gate15-20260907-frozen-windows-installer.json) and the
[corrected Debian lifecycle](gate15-20260907-frozen-debian-installer.json) are separate.
The canary and public release remain later gates. Signing is owner-deferred.
