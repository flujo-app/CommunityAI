# Install CommunityAI

[Download the latest alpha](https://github.com/flujo-app/CommunityAI/releases/tag/v0.1.0-alpha.20260909.2)
for Windows or Ubuntu/Debian. This release includes the simpler desktop and automatic application updates.

**Upgrading from the September 8 release? Run this installer once.** Your settings
and downloaded models are preserved. Future releases download in the app and show
**Restart to update** when ready. You choose when to restart.

| Platform | Small online installer | Complete offline installer |
| --- | --- | --- |
| Windows | [Online setup](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.2/communityai-0.1.0-alpha.20260909.2-windows-online-setup.exe) | [Offline setup](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.2/communityai-0.1.0-alpha.20260909.2-windows-setup.exe) |
| Ubuntu/Debian | [Online installer](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.2/communityai-0.1.0-alpha.20260909.2-linux-online.py) | [Offline .deb](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.2/communityai_0.1.0~alpha.20260909.2_amd64.deb) |

The online installer downloads and verifies the complete offline package. It makes
the initial download smaller; the total runtime download is the same. Model files
download separately when needed. Both platforms include the required runtime
libraries; GPU use still requires a compatible NVIDIA driver.

[Installer SHA-256 checksums](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.2/INSTALLER-SHA256SUMS)
are available for all four downloads.

## Windows

Open the downloaded setup and follow the prompts. It installs for your current
Windows user without administrator elevation. The alpha has no Authenticode
publisher signature, so Windows may show an unknown-publisher prompt.
Launch **CommunityAI** from the Start menu.

## Ubuntu/Debian

Run the downloaded online installer as your normal user:

```sh
python3 communityai-0.1.0-alpha.20260909.2-linux-online.py
```

Or install the downloaded offline package:

```sh
sudo apt install './communityai_0.1.0~alpha.20260909.2_amd64.deb'
communityai
```

The package targets amd64 Debian 12+/Ubuntu 22.04+. Run CommunityAI as your normal
desktop user with an unlocked credential store. Installation and in-app updates
ask for administrator authentication.

## Using and updating the app

Home shows the selected model and why, your hardware, GPU memory budget in GB,
and computing limit. Use **Start sharing** to contribute. The resource controls
default to 100%; you can reduce them before starting. Models expands to show
block health, contributors and your downloads.

The app checks for updates shortly after opening and every six hours. New releases
download automatically with visible progress. **Restart to update** installs the
update after the current answer finishes. Settings, credentials and model caches
are preserved. See [how updates work](AUTOMATIC_UPDATES.md).

Community availability depends on contributors. The app can use its eligible
local fallback when community capacity is unavailable. Computers helping answer
a request may be able to see its contents.

Normal uninstall preserves settings and model downloads. Before uninstalling,
turn off **Start CommunityAI when I sign in**. See
[removal and optional data cleanup](DESKTOP_UNINSTALL.md).

## Release records

The immutable release is `alpha/20260909.2/` on Cloudflare R2.
[Release metadata](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.2/communityai-0.1.0-alpha.20260909.2-release-metadata.zip)
binds the packages to source commit `15e1757ade8881c231db0703a4c1ef01b0dbdd74`.
The R2 development hostname is rate limited.

The source checks and Windows updater handoff fixture passed. Packaging uses the
normal build checks, uploaded size/checksum metadata and public download samples.
Small public files receive complete hash checks. This release does
not claim a new full desktop, GPU, cloud or installed Linux updater test run.
Earlier installer/native-runtime checks remain recorded in
[release readiness](RELEASE_READINESS.md).

Maintainers must retain the update mirror branch and renew its signed feed before
expiry as described in [automatic updates](AUTOMATIC_UPDATES.md). The separately
signed model catalog expires September 28, 2026 at 19:35 UTC; its existing renewal
and branch requirements are recorded in [hosting notes](CLOUDFLARE_RELEASE.md).
