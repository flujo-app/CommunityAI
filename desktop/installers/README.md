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
`CommunityAI.Desktop` stable for public upgrades. Production builds must supply
an approved publisher/signing command instead of `-UnsignedEngineering`; Inno
uses the command for setup and uninstaller signing. See the
[signing decision/application draft](../../docs/WINDOWS_SIGNING.md). Payload
signing and provider enrollment remain required before publication.

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

CI's maintainer address is explicitly an engineering placeholder. Replace it
before publication. A real package must pass installation/upgrade/removal with
the complete runtime on both target distributions. A signed HTTPS APT repository,
its separate GPG key and scoped `Signed-By` installation instructions remain
release work; this builder alone does not provide `apt install communityai`
without a local filename or configured repository.
