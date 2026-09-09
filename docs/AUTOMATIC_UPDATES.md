# Application updates

Installer-managed CommunityAI checks for updates shortly after opening and every
six hours while running. It downloads a newer release automatically. The sidebar
shows download progress and then **Restart to update**. Clicking that button runs
the normal installer and reopens CommunityAI. A current answer must finish first.
Settings, credentials and model caches stay outside the installed program files.

The September 8 release did not contain an updater. Users of that release must
install an updater-enabled release once; subsequent releases use this flow.
Source previews and portable build folders do not update the installed application.

Windows uses the ordinary-user Inno installer and its existing shutdown handshake.
Linux uses the installed, root-owned Python package helper through PolicyKit; the
desktop asks for administrator authentication before APT changes the installation.
The Linux installer is detached from the desktop process tree before APT starts,
so package maintenance cannot terminate its own installer. A cancelled or failed
handoff leaves a retry message when the old desktop remains running.

## Release authentication and recovery

- The executable pins an Ed25519 public key. The update document cannot introduce
  a new signing key or redirect downloads to another host.
- The signed document binds channel, version, monotonically increasing sequence,
  publication/expiry times and each exact package URL, size and SHA-256.
- Expired documents, older sequences, wrong-platform filenames, duplicates and
  unsupported fields are rejected. A lower application version is never installed.
- Interrupted downloads retain their prefix and resume with an exact HTTP range.
  Complete files are checked before becoming ready and rechecked before execution.
- Downloaded installers for the running or older version are removed after a
  successful update check. Cache removal is restricted to updater-owned files.
- The app never closes just because an update becomes available. The user chooses
  when to restart. Errors remain visible with a retry control.

## Publishing

1. Set `desktop/src/communityai_desktop/release.py` to the new release version.
   Build both runtimes and installers from the reviewed commit. Installer versions
   must match the compiled version (`-alpha.` becomes `~alpha.` for Debian).
2. Qualify the packages and upload immutable files to the versioned R2 directory.
   Verify the complete public bytes. Build the online installers from the same
   exact offline package manifest.
3. Run `desktop/installers/sign_app_update.py` with that manifest, release version,
   a strictly increasing sequence and an output path. It reads a base64 Ed25519
   private key from standard input, never from a command-line argument. The local
   release key is protected with Windows DPAPI outside the repository.
4. Publish the signed document **last**, to R2 `updates/alpha.json` with
   `Cache-Control: no-store`. Commit the identical document at
   `public-alpha/updates/alpha.json` on `codex/gate14-20260902-b` for the GitHub
   fallback. Keep that branch available until clients ship a replacement mirror.
5. Verify both public documents with the pinned client key. Publish the GitHub
   prerelease with the download links, small online installers and release hashes.

Feed signatures are valid for 90 days. Renew the signed feed before expiry even
when the package version is unchanged; increment its sequence. Retain the same
key securely. Key rotation requires a release that trusts the replacement key.
This feed signature is separate from Windows Authenticode signing.

The Windows handoff uses documented [Inno command-line options](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)
and [post-install launch behavior](https://jrsoftware.org/ishelp/topic_runsection.htm).
