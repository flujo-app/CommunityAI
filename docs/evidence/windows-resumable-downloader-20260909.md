# Windows resumable downloader — September 9, 2026

**Superseded build record.** A later full transfer with this wrapper stopped on
local progress publication after 541,341,184 bytes; no installer launched. The
[failure remains recorded](normalized-online-windows-resumable-progress-failure-20260909.md).
The [replacement progress fix](windows-resumable-progress-fix-20260909.md) has its
own artifact hashes and tests. The counts and hashes below retain their original
scope.

**Passed for bounded helper and local Inno integration.** A complete R2 download
and real offline-installer handoff remain separate acceptance. The
[machine-readable record](windows-resumable-downloader-20260909.json) binds the
final wrapper, exact source hashes, tests and private publication staging audit.

| Final Windows online artifact | Value |
| --- | --- |
| Filename | `communityai-0.1.0-alpha.20260908.1-windows-online-setup.exe` |
| Bytes | 2,107,444 |
| SHA-256 | `0d1c31206336b1e8c783a78a6cadbdc8f9e643e91d587b88d78581a85b5d9ef6` |
| Embedded Windows-only manifest SHA-256 | `6e4423a2e0316eff9c19c18cf09b18224d8fcc769e6b0f9f10e514a37581355b` |
| Signing | Unsigned alpha |

The wrapper still downloads the unchanged 2,462,345,104-byte offline setup,
SHA-256 `116882e5d94e643e507efedebc4ec4b091275c5703f89bc646957f0f648d64bb`.
That installer has its own [installed native and removal acceptance](normalized-windows-installer-20260908.md).
The combined public manifest is a different file from the exact Windows-only
manifest embedded at build time; both bindings were checked independently.

The small .NET Framework helper resumes interrupted responses using an exact byte
range and checks the response offset, end, total, length and final SHA-256. It
rejects redirects and unexpected encoding, bounds retries and elapsed time, and
observes cancellation and its exact parent process. Inno retains the wizard,
silent behavior, process containment and independent size/hash checks before
starting the offline setup. Atomic progress publication retries brief Windows
sharing conflicts with the wizard's reader. No additional runtime is downloaded.

The Inno bridge compiled with version 6.7.3. The local Setup engine was verified
as PE32/x86; its 68-byte startup, 16-byte process and 112-byte job-limit records
compiled directly. The exact-version guard also rejected a simulated future
version. The helper is assigned to its job before executing download code.
Cancellation and cleanup retain the exact process handle through bounded waits.

## Recorded checks

- **21 helper tests passed in 18.767 seconds.** The saved log's SHA-256 is
  `9f30a84726357ce39c4fb1292ab0c244c13c2f44b3d7b98b89cd95355a186888`.
  Coverage includes real small HTTP interruptions/resume, malformed ranges,
  lengths, redirects, encoding, hashes, finite retry/deadline behavior, parent
  loss, cancellation, progress-reader locks and range arithmetic above 2 GiB.
  A separately compiled production helper rejected loopback HTTP.
- **3 real Inno integration tests passed in 8.894 seconds.** The maintained
  wrapper downloaded a tiny harmless executable from a loopback fixture after
  an observed interrupted-response resume. The child received the exact
  forwarded arguments, remained available until it exited, and its exit code 7
  reached the outer wrapper. Inno then removed its temporary directory.
  Cancellation during a held response and termination of the exact parent
  engine both stopped the helper without launching the child.
- All recorded fixture processes stopped and owned temporary directories were
  cleaned. The fixtures used hidden executables and silent setup; no visible
  windows, product installations, model loads or native credentials were involved.
  Black, isort and whitespace checks passed for the integration test file.

The helper tests are in [test_windows_download_helper.py](../../tests/test_windows_download_helper.py);
the real-wrapper tests are in
[test_windows_online_integration.py](../../tests/test_windows_online_integration.py).
The latter's final result was observed directly in agent execution output; no
separate raw integration log was saved. The JSON distinguishes that observation
from the independently checked saved helper-test log.

## Publication boundary

The private staging audit matched all four intended installer identities and
both platform provenance records. It changed only the Windows online companion
and its installer-checksum entry; the previous companion was preserved outside
the public metadata directories. Each platform folder contains seven release
records, with no raw logs or private data markers found. This audit uploaded
nothing and did not rehash the multi-gigabyte offline installers.

The two earlier full-transfer failures remain recorded
[separately](normalized-online-windows-download-failure-20260908.json),
[including the unchanged-object retry](normalized-online-windows-download-retry-failure-20260908.json).
They failed closed with error 12030; these tests do not establish their cause.
The build companion retains `live_download_verified: false`. A subsequent full
hosted download and actual installer handoff must establish online release
readiness; neither is claimed by this evidence.
