# CommunityAI alpha candidate installation

The September 8 candidates contain the smaller packaged runtime. Windows and
Linux offline installation, native CUDA operations and removal have passed.
Both online installers also passed complete hosted download, verified installer
handoff and removal. All four download options and their release metadata are
published and verified.

The alpha is unsigned. Windows publisher signing, the Microsoft Store, automatic
application updates and a hosted APT repository follow after alpha. The model
catalog is separately signed and verified by the application.

## Candidate downloads and acceptance

| Candidate | Download bytes | Status |
| --- | ---: | --- |
| [Windows x64 offline setup](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai-0.1.0-alpha.20260908.1-windows-setup.exe) | 2,462,345,104 | Published. Complete hosted download/hash, ordinary-user installation and removal passed; native CUDA checks passed separately for this exact setup. |
| [Debian/Ubuntu amd64 package](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai_0.1.0~alpha.20260908.1_amd64.deb) | 2,302,428,788 | Full hosted download/hash, protected-copy/APT installation and removal passed on Ubuntu 22.04; native checks passed separately for the exact package. |
| [Windows online setup](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai-0.1.0-alpha.20260908.1-windows-online-setup.exe) | 2,107,751 | Published and public file hash verified. Complete hosted download/hash, ordinary-user installer handoff, installed CPU diagnostic and removal passed. |
| [Linux online installer](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai-0.1.0-alpha.20260908.1-linux-online.py) | 13,662 | Published and public file hash verified. Actual HTTPS → protected copy → APT installation and removal passed. |

Compare the downloaded file's SHA-256 with the value below before running it.
The [installer checksums](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/INSTALLER-SHA256SUMS),
[release manifest](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/release-downloads.json)
and [metadata ZIP](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai-0.1.0-alpha.20260908.1-release-metadata.zip)
are public. Complete anonymous downloads matched all 24 small release objects,
including the online installers and 19 curated platform records. Both large
hosted installers passed full download/hash checks during actual installation;
an HTTP HEAD response alone would not establish their integrity.
[Publication record](evidence/alpha-cloudflare-publication-20260909.json).

Both runtime bundles identify source
`84205f93fc73d3babd39e238944b97fab0d11b3e`. All nine CI checks passed at
`fdd8d0b799e42b307450b0f0776a88b4aeffe852`, an import-formatting follow-up;
those CI outputs retain their own provenance and predate the new resumable
Windows helper. The online helper, Inno script and builder match source commit
`b6c8aad9cea208630785d890cfb966093f809e7e`, verified after their working-tree build.
Its 24 helper tests and four real Inno fixture tests passed separately. The
[Windows acceptance](evidence/normalized-windows-installer-20260908.md) and
[Linux acceptance](evidence/alpha-normalized-linux-20260908.md) bind the new
installers to their actual installed runtime and cleanup.

Runtime file content is 4,263,859,354 bytes on Windows and 5,161,115,250 bytes on
Linux, approximately 4.26 GB and 5.16 GB. Installation metadata, filesystem
allocation and temporary upgrade files add overhead. Allow room for the
compressed installer and model cache as well. Model weights download separately
on demand. The [packaging changes](evidence/runtime-packaging-reduction-20260908.md)
remove unused bitsandbytes CUDA variants and reduce large duplicate Linux native
libraries while retaining the required loader paths.

The [earlier Gate 14 resource acceptance](evidence/gate14-20260907-final-resource-acceptance.md)
and [Gate 15 installer/login acceptance](evidence/gate15-20260908-final-installer-acceptance.md)
remain the baseline for their recorded Windows/Debian/Ubuntu scopes. They are
separate from the new candidate checks above.

## Windows

Download the offline setup linked above. In PowerShell, from its folder:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\communityai-0.1.0-alpha.20260908.1-windows-setup.exe
```

Expected SHA-256:

```text
116882e5d94e643e507efedebc4ec4b091275c5703f89bc646957f0f648d64bb
```

Open setup and follow the prompts. It installs for your current Windows user
without administrator elevation. Windows may identify the publisher as unknown
because this alpha has no Authenticode signature. Launch **CommunityAI** from the
Start menu after installation.

Run the next published setup to upgrade. Setup stops the current desktop and its
owned node before replacing application files. Remove CommunityAI through
Windows installed-app settings to uninstall it.

## Debian/Ubuntu

The new package passed installed acceptance on Ubuntu 22.04 and is available
above. Verify it and install it with APT:

```sh
sha256sum 'communityai_0.1.0~alpha.20260908.1_amd64.deb'
```

Expected SHA-256:

```text
714b9a7ac9121f3cf3b85f9677d541b488c2f00020bc6e1ce73ba7081f851576
```

After comparing the checksum:

```sh
sudo apt install './communityai_0.1.0~alpha.20260908.1_amd64.deb'
communityai
```

The package targets amd64 Debian 12+/Ubuntu 22.04+. Run CommunityAI as your
ordinary desktop user with an unlocked native credential store. GPU use requires
a compatible NVIDIA driver; the packaged CUDA libraries do not install a system
driver. Tested platform boundaries remain in [release readiness](RELEASE_READINESS.md).

Install the next downloaded `.deb` with `sudo apt install ./<new-package>.deb`
to replace it. Remove the application with `sudo apt remove communityai`.
Package maintenance stops processes belonging to its installation before
replacement or removal and refuses to proceed if ownership or shutdown cannot
be verified.

## Online installers

Online installers download the complete offline package, verify its exact size
and SHA-256, then invoke the ordinary installer. They reduce the initial
download, not the total runtime download.

Windows's resumable installer passed a complete public HTTPS download, exact
size/hash verification, ordinary-user installation, an installed CPU diagnostic
and uninstall. Its separate online/offline logs, retained child-process handle,
temporary-file cleanup and persisted user-state baseline matched the
[acceptance record](evidence/normalized-online-windows-installer-20260908.md).
The same offline payload passed the earlier required-CUDA/NF4 checks. The three
earlier failed transfers remain recorded; this one successful transfer does not
establish broad network availability. The accepted online setup's SHA-256 is:

```text
8ad0b7da83fdc7c32223902ed13d51f5f2946c89e00947e50aae742c1fc37861
```

Download the Windows online setup linked above, compare that checksum, then open
it. It downloads the full runtime before starting the ordinary setup prompts.

Linux's actual ordinary-user download → protected copy → APT installation and
removal passed the [saved log and state audit](evidence/alpha-online-linux-hosted-20260908.md).
The raw diagnostic harness missed process arguments; APT's own package path and
transaction log supplied the input/version binding. Prior native checks cover
the byte-identical offline package. The published script's SHA-256 is:

```text
59a00906d358c1cac0046e9c323a612e7f6fdb824d21ba562de0bae18ba39b0b
```

After downloading and comparing that checksum, run it as your ordinary user:

```sh
python3 communityai-0.1.0-alpha.20260908.1-linux-online.py
```

See the
[Windows](../desktop/installers/ONLINE_WINDOWS.md) and
[Linux](../desktop/installers/ONLINE_LINUX.md) instructions for the supported
options. Linux's online flow needs temporary room for two compressed packages
in addition to the installed runtime and package-manager overhead.

## First launch and retained data

CommunityAI fetches its signed catalog on first launch. Both resource sliders
start at 100%; sharing begins only when you enable it. Set VRAM and processing
usage before contributing. Processing limits pace contributed work and allow
brief compute bursts. Local inference and local model downloads are separate.

Community inference is best effort. Current complete public Qwen route capacity
has not been verified by this release check; historical successful routes do not
guarantee a route is available now. The application reports unavailable capacity
and can use its eligible local fallback. Computers helping with a request may
be able to see its content.

Upgrade and normal uninstall preserve settings, native credentials and model
caches outside the application directory. Before uninstalling, turn off
**Start CommunityAI when I sign in**: the alpha uninstaller retains that per-user
login entry. Follow the [explicit cache, credential and full-reset choices](DESKTOP_UNINSTALL.md)
to remove retained data intentionally.

## Release maintenance

The immutable candidate prefix is
`https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/`.
The initial R2 hostname is rate-limited; it provides no availability guarantee.
[Hosting and publication records](CLOUDFLARE_RELEASE.md) track the exact objects,
manifests, passed download acceptance and completed artifact publication.

At **2026-09-08 23:52 UTC**, the public catalog, bootstrap and both model manifests
were reachable and passed the existing signature/digest verifiers. The signed
catalog expires on **2026-09-28 at 19:35 UTC** and must be renewed before then.
Its published URLs depend on `codex/gate-v-auto-selection`; maintainers must
preserve that branch or migrate the endpoints before removing it.
[Metadata check](evidence/alpha-public-metadata-20260908.json).

Qualified candidate links and a draft release can proceed while Gate 16's scope
review continues. The combined public canary has not run and is not claimed as
passed. [Current release status](RELEASE_READINESS.md).
