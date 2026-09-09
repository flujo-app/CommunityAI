# Windows downloader progress fix — September 9, 2026

**24 helper tests and four real Inno tests passed.** A complete hosted Windows
download and actual offline-installer handoff remain separate acceptance. The
[JSON record](windows-resumable-progress-fix-20260909.json) binds this replacement
wrapper, exact source hashes, saved test logs and the private metadata audit.

| Replacement Windows online artifact | Value |
| --- | --- |
| Filename | `communityai-0.1.0-alpha.20260908.1-windows-online-setup.exe` |
| Bytes | 2,107,751 |
| SHA-256 | `8ad0b7da83fdc7c32223902ed13d51f5f2946c89e00947e50aae742c1fc37861` |
| Embedded Windows-only manifest SHA-256 | `6e4423a2e0316eff9c19c18cf09b18224d8fcc769e6b0f9f10e514a37581355b` |
| Signing | Unsigned alpha |

The previous resumable wrapper stopped after 263.343 seconds with 541,341,184
bytes reported, because its progress record could not be published. No offline
installer launched; the recorded processes and temporary files were cleaned and
the persisted baseline was restored. The underlying exception was not recorded,
so no specific Windows error or network cause is inferred. That
[actual failed attempt](normalized-online-windows-resumable-progress-failure-20260909.md)
and the [earlier bounded checks](windows-resumable-downloader-20260909.md) remain
separate historical evidence.

Progress publication is now best effort, including its final frame. Sharing
conflicts receive at most 75 ms of retries; other presentation I/O failures skip
the frame and may write a sanitized exception type and HRESULT. Inno can display
the actual partial-file length when the progress record is stale. Cancellation,
parent-process ownership, transfer limits and complete size/SHA-256 checks remain
authoritative. The helper must verify the download before returning success;
Inno independently verifies size and SHA-256 before logging completion or
launching the unchanged offline setup.

The final helper suite passed **24 tests in 22.299 seconds**. Added cases keep a
reader open without delete sharing through helper exit, make the progress file
read-only to produce access denial, and combine that denial with an incorrect
download hash to confirm rejection. Existing response-range, retry, cancellation,
parent-loss, deadline and production-HTTPS checks remain covered.

The actual Inno suite passed **four tests in 14.589 seconds**. The new case holds
the progress file without delete sharing for at least 1.4 seconds during a slow
local download and observes a skipped-publication warning. The verified harmless
child still receives the exact forwarded arguments, remains available until it
exits, and its exit code 7 reaches the wrapper. Inno then removes the temporary
directory. The prior interrupted-resume, cancellation and exact-parent-death
cases also passed. All recorded owned processes stopped; these tests used hidden
executables and silent setup, with no product installation or visible window.

Both saved logs and the exact test/source hashes were checked for this record.
The helper source SHA-256 is
`3bfa36e62f2a160b2e258af6d9d6816e815d8a9c5b918c29dd800eb6e6c20d8a`;
the Inno script SHA-256 is
`d975b6da3a1067e679d9ba55555fa5b6219ae644fce8050225486909600fbbb1`.
These identify the working-tree build inputs; the offline runtime's source
commit does not identify the newer online wrapper. A subsequent source commit,
`b6c8aad9cea208630785d890cfb966093f809e7e`, contains independently verified matching
Git blobs for the helper, Inno script and builder. The staged
`metadata/windows/online-source-commit.json` records that binding; it does not
claim the original build used a clean committed checkout. Black, isort and
whitespace checks passed for the integration test file.

The private publication audit matched all four installer identities, both
platform provenance records, the combined manifest and the distinct Windows-only
manifest. It updated only the Windows online companion and its checksum entry,
preserving the previous companion outside the public metadata folders. Eight
Windows records and nine Linux records matched the explicit metadata allowlists;
the two Linux acceptance records matched their sanitized documentation copies.
No raw logs or private-data markers were found in those metadata files. Linux
metadata and the other three installer checksum entries were unchanged.

This check uploaded nothing, rehashed no multi-gigabyte offline installer, and
performed no full public transfer, model load, peer contact or credential access.
The replacement companion retains `live_download_verified: false`; these local
passes do not establish full online release readiness.
