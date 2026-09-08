# CommunityAI alpha candidate installation

These instructions describe the qualified September 7 candidates. They are ready
for release preparation; a public download location has not been published.
The unchanged Debian package also passed the
[Ubuntu 22.04 installed lifecycle on September 8](evidence/gate15-20260908-frozen-ubuntu-installer.md).
Current acceptance and remaining release work are tracked in
[RELEASE_READINESS.md](RELEASE_READINESS.md).

The alpha is unsigned. Windows publisher signing, the Microsoft Store, automatic
application updates and a hosted APT repository follow after alpha. The model
catalog is separately signed and verified by the application.

## Candidate downloads

| Platform | Installer | Download bytes |
| --- | --- | ---: |
| Windows x64 | `communityai-0.1.0-alpha.20260907.2-windows-setup.exe` | 2,519,046,440 |
| Debian/Ubuntu amd64 | `communityai_0.1.0~alpha.20260907.4_amd64.deb` | 3,781,591,484 |

Download the installer and its `INSTALLER-SHA256SUMS` from the same official
release. Compare the checksum with the release notes before running it. The
[artifact inventory](evidence/alpha-artifact-audit-20260907.json) records the exact
files, hashes, source provenance and lifecycle evidence. Model weights are
downloaded separately on demand; they are not inside the installers.

The unpacked runtime occupies approximately 4.5 GB on Windows and 8.6 GB for this
Linux candidate. Allow additional space for the downloaded installer, model
cache and temporary upgrade files. These are measured runtime sizes, not a
minimum disk-space guarantee.

## Windows

In PowerShell, from the folder containing the download:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\communityai-0.1.0-alpha.20260907.2-windows-setup.exe
```

The expected SHA-256 is:

```text
c4e8df599f3a6118eab5718a5ad50655b0e07fd6c270aacf7dbb0b3065c5c399
```

Open the setup file and follow the installation prompts. It installs for your
current Windows user without administrator elevation. Windows may identify the
publisher as unknown because this alpha does not have an Authenticode signature.
Launch **CommunityAI** from the Start menu after installation.

Run the next published setup file to upgrade. Setup stops the current desktop and
its owned node before replacing application files. Remove CommunityAI through
Windows installed-app settings to uninstall it.

## Debian/Ubuntu

This package targets amd64 on Debian 12+/Ubuntu 22.04+. Its actual acceptance
scope is listed in the release readiness record; a physical desktop or GPU that
has not been tested is not implied by that baseline.

From the download folder:

```sh
sha256sum 'communityai_0.1.0~alpha.20260907.4_amd64.deb'
```

The expected SHA-256 is:

```text
a2cc0548cd51f98ed7a9c208be18b53a701a9317cbc63293d4bf7d1e14151517
```

Install the local package so APT also resolves its declared desktop dependencies:

```sh
sudo apt install './communityai_0.1.0~alpha.20260907.4_amd64.deb'
communityai
```

Run the application as your ordinary desktop user. Package installation and
removal use administrator privileges. An unlocked native desktop credential
store is required. GPU use also requires a compatible NVIDIA driver; the
packaged CUDA libraries do not install a system driver.

Install the next downloaded `.deb` with `sudo apt install ./<new-package>.deb`
to replace it. Remove the application with:

```sh
sudo apt remove communityai
```

The package stops processes belonging to its installation before replacement or
removal. It refuses to proceed if it cannot verify process ownership or finish
shutdown.

## First launch and retained data

CommunityAI fetches its signed model catalog on first launch. Sharing is opt-in:
both resource sliders start at 100%, and contributing begins only when you enable
it. Set VRAM and processing usage to your preferred values before starting.
The processing slider paces contributed work; it is not an instantaneous cap on
whole-device utilization. The local model download appears separately from
sharing-worker downloads.

Settings, native credentials and model caches live outside the application
installation. Upgrade and normal uninstall preserve them so reinstall can reuse
your choices and verified downloads. Application removal does not mean those
retained files have been erased.

Before uninstalling, turn off **Start CommunityAI when I sign in**. The alpha
uninstaller does not remove a saved per-user login entry. Follow the
[explicit cache, credential and full-reset choices](DESKTOP_UNINSTALL.md) to
remove retained data intentionally.

## Publication requirement

Both candidate installers exceed GitHub Releases' **2 GiB per-file limit**.
GitHub release notes can link to them, but the complete files need a different
download origin. [GitHub's release limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas).

For a single-file download, the preferred candidate is **Cloudflare R2 Standard
storage**, with immutable versioned object paths and a custom download domain.
The two installers total **6,300,637,924 bytes** (about 6.3 GB). That fits R2's
10 GB-month included storage allowance if other account usage and retained
versions leave enough room. Standard storage includes 1 million Class A and
10 million Class B operations monthly, and direct R2 downloads have no egress
charge. These allowances make a small alpha plausibly free; they are not a
guarantee of the account's bill. [R2 pricing](https://developers.cloudflare.com/r2/pricing/).

Both files fit the documented 5 GiB single-part object upload limit. Use a custom
domain for the public release: the supplied `r2.dev` hostname is rate limited
and intended for development. Account/R2 enablement, domain control, bucket
configuration and an anonymous download/checksum test are still required; no
Cloudflare account or hosting setup was inspected or created by this audit.
[R2 limits](https://developers.cloudflare.com/r2/platform/limits/),
[public bucket domains](https://developers.cloudflare.com/r2/buckets/public-buckets/).

Publish the exact qualified installer bytes there, then put their permanent
links, checksums and provenance in the GitHub release. Keeping a second full
6.3 GB release alongside this one would exceed the included storage allowance
if retained for a full month, before any other account usage.

Google Cloud Storage is an alternative in the existing CommunityAI GCP project
and supports objects up to 5 TiB. That project currently documents a discovery
VM, not an installer download service. A read-only bucket inventory could not
authenticate during this audit; no download bucket, public access or cost
authorization has been established. [Cloud Storage object limits](https://docs.cloud.google.com/storage/quotas#objects).

Splitting the unchanged files into sub-2-GiB GitHub assets and reconstructing them
locally is another option, but introduces a download/reassembly step before
setup. It is not the single-file installation flow described above. No installer
or public hosting configuration was changed by this audit.
