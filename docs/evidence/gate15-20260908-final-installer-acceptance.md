# Gate 15: installer and login-startup acceptance

**PASSED for the bounded Windows/Debian/Ubuntu alpha scope, September 8, 2026.**
The final Windows frozen sign-in cycle closes the remaining installer gate.
This acceptance combines the following real runs; their original failures and
scope limits remain in the individual records.

| Outcome | Tested result |
| --- | --- |
| Windows setup lifecycle | Non-elevated Windows 10 Pro 19045: installation, upgrade from an earlier full setup while sharing was active, removal, reinstall and final removal. Three installed GUI/node launches generated local Qwen tokens and verified sharing artifacts. Settings/cache/credential retention and independent cleanup passed. [Evidence](gate15-20260907-frozen-windows-installer.json). |
| Debian setup lifecycle | Debian 12, ordinary-user Xvfb/native Secret Service and CUDA passthrough: install, active same-version replacement, removal, reinstall and final removal. All three installed launches generated local tokens and verified a sharing block; complete process shutdown, retention and independent cleanup passed. [Evidence](gate15-20260907-frozen-debian-installer.json). |
| Ubuntu setup lifecycle | Ubuntu 22.04 in a disposable local WSL2/Docker container, two CPU cores and 6 GiB RAM: the same unchanged `.deb` passed the full lifecycle and all three real inference/artifact checks. Independent cleanup passed and the container was removed. Initial installation took 329.971 seconds. [Evidence](gate15-20260908-frozen-ubuntu-installer.md). |
| Frozen Windows sign-in control | The literal checkbox changed Off→On; a new GUI retained On and changed it to Off. The exact frozen executable's `REG_SZ` startup command was verified. Both normal shutdowns preserved configuration/native credential, loaded no models and left empty jobs. Independent audit confirmed all 18 identities gone, credential removed and original Run state restored. [Evidence](gate15-20260908-frozen-windows-login.md). |
| Frozen Linux sign-in control | Literal checkbox enable, new GUI reading enabled state, disable, and an explicit login-flag launch passed on private Xvfb/AT-SPI. The exact XDG entry, credential continuity and all three normal shutdowns passed. Independent cleanup passed. [Evidence](gate15-20260908-frozen-linux-login.md). |
| Manual retained-data choices | Retain, cache-only deletion and full reset passed on disposable Windows state. The actual frozen credential-deletion command passed; unrelated state was preserved. [Evidence](gate15-20260908-windows-data-login-choices.json). |
| Signed catalog startup migration | Normal frozen Windows and Linux startup independently migrated signed sequence 1 to exact sequence 2, preserving preferences/cache/credential and rejecting the old trust root. [Windows](qwen-catalog-desktop-20260907.md), [Linux](qwen-catalog-linux-startup-20260908.md). |

The [machine-readable record](gate15-20260908-final-installer-acceptance.json)
binds the component evidence and qualified artifact identities. The candidates are:

| Installer | SHA-256 |
| --- | --- |
| `communityai-0.1.0-alpha.20260907.2-windows-setup.exe` (2,519,046,440 bytes) | `c4e8df599f3a6118eab5718a5ad50655b0e07fd6c270aacf7dbb0b3065c5c399` |
| `communityai_0.1.0~alpha.20260907.4_amd64.deb` (3,781,591,484 bytes) | `a2cc0548cd51f98ed7a9c208be18b53a701a9317cbc63293d4bf7d1e14151517` |

Windows runtime source is `76b6d84`; Linux runtime source is `bf67f0d`.
Linux maintenance controls come from `61ab7b1`, with the compressed runtime
payload unchanged. Later CI builds remain distinct artifacts.

The Windows lifecycle's redundant final driver-cleanup error, two earlier Debian
shutdown failures, Ubuntu's first unpack timeout, the Linux accessibility actor
and display-wrapper errors, and the first Linux catalog-startup failure remain
linked in their records. The Windows reader's four harness failures and passing
read-only prerequisite are also [preserved separately](gate15-windows-private-read-20260908.md).
No failed attempt is reclassified as a pass.

Users must **disable sign-in startup before uninstalling**, and perform native
credential reset while the executable remains installed. The alpha uses explicit
manual cache/state deletion; uninstall retains those files and does not remove
the per-user login entry automatically. Follow the [removal runbook](../DESKTOP_UNINSTALL.md).

Linux acceptance covers the tested container/Xvfb/NVIDIA profiles and same-version
replacement; Windows additionally covers a different-version upgrade. This does
not qualify physical Wayland sessions, other GPUs, Ubuntu 24.04, macOS, actual
OS logout/login, or unobserved tray/minimized presentation. Frozen periodic catalog
activation during generation and broader Qwen measurements remain separate.

Unsigned setup is owner-authorized for alpha. Publisher signing, Store, hosted
signed APT and automatic software updates follow after alpha. Gate 16's live
canary and Gate 17 publication/observation remain open; these installers are
qualified candidates, not a published public service.
