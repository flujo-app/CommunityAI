# CommunityAI engineering installers

These builders package the already verified output of `desktop/build_desktop.py`.
Run its independent checksum/provenance verification before invoking either
builder. CI does this and uploads separate `communityai-setup-windows` and
`communityai-setup-linux` artifacts. They are **unsigned engineering builds**,
not a release qualification or Store submission.

## Windows

Use Inno Setup 6 and PowerShell, from the repository root:

```powershell
./desktop/installers/build_windows_installer.ps1 `
  -Bundle desktop/dist/desktop/CommunityAI `
  -OutputDirectory desktop/dist/installers `
  -Version 0.1.0-alpha.1 -Compiler 'C:/path/to/ISCC.exe' -UnsignedEngineering
```

The installer defaults to the current user's LocalAppData Programs directory and
requires no elevation. Silent install uses `/VERYSILENT /SUPPRESSMSGBOXES
/NORESTART`; the uninstaller accepts the same options. Re-run the next installer
to upgrade. Before replacement/removal, the installed `CommunityAI.exe
--prepare-update` asks the same user's desktop to quit and waits for owned-node
cleanup and release of its instance lock. Failure stops replacement. Existing
unmarked application directories are refused; use a new directory for a prior
unpacked engineering archive.

The installer removes obsolete `_internal` and `node` files only inside its
marked installation directory. Settings, credentials and model cache live
outside this directory and are retained. Explicit cache deletion and login-entry
cleanup are not implemented by this installer yet.

`-AppIdentifier` exists for isolated engineering installations. Keep the default
`CommunityAI.Desktop` stable for public upgrades. The owner has approved unsigned
direct-download installers for the alpha, labelled accordingly and accompanied
by verified checksums/provenance. Later signed builds supply an approved
publisher/signing command instead of `-UnsignedEngineering`; Inno uses the
command for setup and uninstaller signing. See the
[signing decision/application draft](../../docs/WINDOWS_SIGNING.md). Payload
signing and provider enrollment remain required before Store submission.

## Ubuntu/Debian

Build on Ubuntu 22.04 or a compatible baseline with `dpkg-deb`:

```sh
python desktop/installers/build_deb.py \
  --bundle desktop/dist/desktop/CommunityAI \
  --output desktop/dist/installers --version '0.1.0~alpha.1' \
  --maintainer 'Maintainer Name <real-contact@example.org>'
sudo apt install ./desktop/dist/installers/communityai_0.1.0~alpha.1_amd64.deb
```

The package owns `/opt/communityai`, `/usr/bin/communityai` and the system menu
entry. `preinst`/`prerm` stop processes whose executables belong to the marked
installation and their observed descendants, using kernel PID handles and
process start times. They fail if matching processes remain. They never enumerate
or remove home-directory settings/cache. Python 3.9+ and Linux PID handles are
required; the supported baseline is Ubuntu 22.04+/Debian 12+ on amd64.
The declared Qt/X11 dependencies include `libxcb-shape0`; omitting it prevented
the frozen desktop opening on a minimal Debian host even though offscreen tests
passed. CI now also opens the frozen UI and onboarding through X11/Xvfb.
Frozen Linux packages use the approved eager/native inference kernels and exclude
the optional Triton JIT, so importing adapter support does not require a compiler
or Python development headers on contributors' computers. Source deployments can
install Triton separately for execution profiles that need it.

CI's maintainer address is explicitly an engineering placeholder. Replace it
before publication. A real package must pass installation/upgrade/removal with
the complete runtime on both target distributions.

## Signed APT repository

APT repository signing uses GPG and does not require a commercial certificate.
Use a separate protected repository signing key, not the model-catalog key. With
Python 3.11+, `apt-utils`, GnuPG and the key available in the operator's GPG home:

```sh
python desktop/installers/build_apt_repository.py \
  --package desktop/dist/installers/communityai_0.1.0~alpha.1_amd64.deb \
  --output desktop/dist/apt-release-1 --signing-key FULL_KEY_FINGERPRINT
```

Repeat `--package` for retained versions. The output directory must be new. The
builder writes versioned package paths, `Packages`, `Packages.gz`, `Release`,
`InRelease`, `Release.gpg`, the exported public key and a hash inventory. Both
signatures are independently checked with `gpgv`. It never publishes output or
creates a signing key. Repository metadata expires after 30 days; re-sign and
publish it before expiry even when package versions do not change.

Publish immutable package paths first, then index files, then signed release
metadata at the chosen HTTPS origin. Preserve existing versioned package URLs.
Distribute the public key and full fingerprint through the project download
page; users install it as `/etc/apt/keyrings/communityai.gpg` and use a deb822
`.sources` entry with `Types: deb`, the actual HTTPS `URIs`, `Suites: alpha`,
`Components: main`, `Architectures: amd64` and
`Signed-By: /etc/apt/keyrings/communityai.gpg`. Keep the key readable by APT and
scope package pinning to this repository and the `communityai` package as in
[Debian's third-party guidance](https://wiki.debian.org/DebianRepository/UseThirdParty).

Production key custody/backup, a permanent HTTPS origin, final setup commands and
publication remain open. A disposable container test proved signed index
acceptance, package candidate/download/hash verification and tampered-signature
rejection; it created no production key or public repository.
