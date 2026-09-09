# Cloudflare alpha downloads

The owner activated R2 on September 8, 2026. The `communityai-releases` bucket
uses Standard storage. Its initial test origin is
`https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev`.
The fresh account has no custom domain. Cloudflare rate-limits `r2.dev` and
intends it for development; use a custom domain for wider distribution.
[Public bucket guidance](https://developers.cloudflare.com/r2/buckets/public-buckets/).

The owner authorized a personal API token named **CommunityAI release storage
CLI**, restricted to **Workers R2 Storage: Edit** on this account. It is stored
outside the repository using Windows DPAPI for the current user. No credential
is embedded in an installer, release manifest, GitHub secret, or this document.

Wrangler **4.130.0** created the bucket and enabled its public URL. AWS CLI
**1.46.1** uses the same authorized token through R2's S3-compatible interface.
Both clients uploaded harmless test objects, and anonymous HTTPS requests
returned their exact content with HTTP 200. This establishes hosting access;
it does not establish installer download or installation acceptance.

Wrangler's object uploader limits individual uploads to 300 MiB. Use the S3 CLI
for the multi-gigabyte installers. The local upload profile limits concurrency
to two requests and uses 16 MiB multipart chunks above a 64 MiB threshold.
Cloudflare documents deriving the S3 access key ID from the token ID and its
secret from SHA-256 of the token value; keep both values private.
[R2 authentication](https://developers.cloudflare.com/r2/api/tokens/),
[large-object uploads](https://developers.cloudflare.com/r2/objects/upload-objects/).

## Publication sequence

1. Build and qualify the normalized offline EXE and Debian package. Preserve the
   previously qualified files and record each replacement's version, exact byte
   count, SHA-256 and source provenance.
2. Choose a new immutable release prefix, such as `alpha/20260908.1`. Check that
   its objects are absent; do not overwrite a published version.
3. Upload only the named release artifacts with the S3 CLI. Check each object's
   byte count and anonymously download/hash the resulting HTTPS object.
   Quote the PowerShell cache argument as
   `--cache-control 'public,max-age=31536000,immutable'`. Finalize object metadata
   before starting qualification downloads, then leave both payload and metadata
   unchanged while downloads are active.
4. Create `release-downloads.json` with
   `desktop/installers/release_downloads.py create`, supplying the real HTTPS
   base URL and each exact offline file/version. Build the Windows and Linux
   online installers from that pinned manifest.
5. Qualify actual downloader-to-installer handoff, then upload the small online
   files, checksums and release metadata. Publish their verified links in the
   release notes and installation guide.

The versioned release prefix is `alpha/20260908.1/`. Both offline installers are
uploaded and passed complete hosted download/hash verification. Windows's
2,462,345,104-byte setup passed actual ordinary-user handoff through the updated
online wrapper, installed CPU diagnostics and removal. The retained offline-child
handle returned exit 0; temporary files were removed and the saved user-state,
menu and native-registration baselines matched after uninstall.
[Windows hosted acceptance](evidence/normalized-online-windows-installer-20260908.md).
Prior installed CUDA/NF4 checks cover this exact offline package.

The published [Windows online setup](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai-0.1.0-alpha.20260908.1-windows-online-setup.exe)
is 2,107,751 bytes, SHA-256
`8ad0b7da83fdc7c32223902ed13d51f5f2946c89e00947e50aae742c1fc37861`.
Its complete anonymous public body matched that checksum. The
original downloader's two connection failures and the first resumable helper's
fatal progress update remain separate evidence. The current helper treats
progress records as optional while retaining independent complete-file checks;
an observed skipped progress frame did not interrupt the successful handoff.

Linux's 2,302,428,788-byte hosted package passed full download/hash,
protected-copy/APT installation and removal, using the
[scoped log/state audit](evidence/alpha-online-linux-hosted-20260908.md).
Its 13,662-byte online script and nine curated Linux metadata files are published.
Every public small-file body was downloaded anonymously and matched to its
local SHA-256. Prior native CPU/CUDA checks cover the exact offline package.
Keep each platform's `provenance.json`, `desktop-metrics.json`, `SHA256SUMS` and
companions under `metadata/windows/` or `metadata/linux/`. Those generic names
must not overwrite the other platform's records. The Windows online builder
used the exact `metadata/windows/windows-release-downloads.json`; preserve its
bytes because the executable's companion binds the whole manifest hash. The
combined `release-downloads.json` lists both exact offline artifacts, while
`INSTALLER-SHA256SUMS` covers both offline and both online files. Build companions
record compilation with `live_download_verified=false`; the separate acceptance
records establish later actual download and installation results.

At **2026-09-09 02:02 UTC**, the final
[publication audit](evidence/alpha-cloudflare-publication-20260909.json) matched
all 24 small public object bodies: both online installers, 19 curated platform
records, the combined manifest, installer checksums and metadata ZIP. The
[installer checksums](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/INSTALLER-SHA256SUMS),
[combined manifest](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/release-downloads.json)
and [metadata ZIP](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai-0.1.0-alpha.20260908.1-release-metadata.zip)
are available. The ZIP is 1,009,867 bytes, SHA-256
`9e3424eb8587b09b02b464dc2456aecd1150f154c4fe719069e39374129bb60b`.
Both offline objects also returned HEAD 200 with exact lengths; their complete
byte identities come from the actual online acceptance records. This publishes
the qualified candidate downloads; it does not claim Gate 16's combined canary
has run or establish broad service availability.

Both offline runtimes identify source
`84205f93fc73d3babd39e238944b97fab0d11b3e`. The Windows online helper, Inno script
and builder were built from working-tree files and subsequently matched to all
three Git blobs at `b6c8aad9cea208630785d890cfb966093f809e7e`.
`metadata/windows/online-source-commit.json` records that separate binding;
neither source identity substitutes for the other.

Harmless probes under `_checks/` are not release installers. The complete new
offline installers total 4,764,773,892 bytes, down from 6,300,637,924 bytes. Avoid
retaining multiple full releases without checking account storage usage.
R2's included usage is an allowance, not a spending cap.
[Pricing](https://developers.cloudflare.com/r2/pricing/).

The original and revised Windows downloaders passed real HTTPS negative checks: it rejected a
harmless 52-byte object with a deliberately wrong embedded hash and exited with
code 1, without launching a child installer. Its temporary directory was removed.
[Original check](evidence/alpha-online-windows-https-rejection-20260908.json),
[current progress-fix check](evidence/alpha-online-windows-progress-fix-https-rejection-20260909.json).
