# Linux online installer

This builder creates a small standalone Python 3.9+ script. It downloads one
explicitly pinned full `.deb`, including the packaged CPU/NVIDIA runtime, and
installs it through APT. It uses the same package, worker shutdown and retained
settings/cache behavior as the offline installer. It does not resolve new pip
dependencies, download model weights, add a repository or install a GPU driver.

Build from the repository with the shared release manifest:

```sh
python3 desktop/installers/build_linux_online.py \
  --manifest release-downloads.json \
  --output communityai-linux-online.py
```

The manifest must contain a validated `linux-amd64` offline-installer entry from
`release_downloads.py`. The generated script embeds its immutable HTTPS URL,
version, filename, byte size and SHA-256. Its adjacent `.py.json` records the
script's own identity and the pinned offline entry. Existing output files are
never replaced. Use the actual release's immutable object URL; placeholder
fixtures are not publication artifacts.

Download the generated script as a file from the official release, verify its
published checksum, then run it as your ordinary desktop user:

```sh
python3 communityai-linux-online.py
```

Do not pipe a remote response into a shell or run the downloader itself with
sudo. It requests sudo only after checking the complete download. The elevated
helper runs isolated system Python, copies the package into its own root-owned
directory, and rechecks the pinned size/hash while copying. APT receives that
protected copy, so changing the original file during the password prompt cannot
change what APT installs. APT keeps its normal dependency resolution and prompts.

The default staging parent is `/var/tmp`. Allow temporary space for **two copies
of the compressed package**, plus the unpacked application and any dependency or
upgrade overhead. For the qualified September 8 package (2,302,428,788 bytes),
this means about 4.60 GB of
package staging, in addition to the installed runtime. The wrapper checks two
package sizes plus a 64 MiB margin in the selected staging directory; the helper
also checks room for its copy in `/var/tmp`. These checks do not promise enough
space for all APT operations. `--directory PATH` selects another existing parent
for the ordinary-user download; the protected copy remains in `/var/tmp`.

`--download-only` retains the exact verified `.deb` and prints its path without
requesting sudo. It needs one package's temporary space. The same file can later
be used with the documented offline installation procedure.

Downloads use HTTPS certificate validation, reject redirects and encoded bodies,
and require the exact byte count and SHA-256 before elevation. They have a
30-second blocking network-operation timeout and a two-hour Linux signal
deadline. The protected local copy also has a two-hour deadline. These deadlines
are disarmed before APT starts. Cancellation or verification failure removes
only staging created by that invocation. If APT's exit cannot be confirmed, its
input is retained rather than removed or the package manager killed; the path is
printed for later review. Package-manager recovery follows APT's own messages.

Fixture validation covers download, cancellation, protected-copy ordering,
changed-source rejection, APT exit handling, and a generated script's standalone
`--help` using inert fixtures. The focused suites also passed inside the cached
Ubuntu 22.04 container with two CPU cores, a 2 GiB memory cap, read-only sources
and no network. Linux's source-symlink rejection passed there. These checks
never invoked real sudo or APT.

The release's actual hosted download, real sudo/protected-copy/APT installation
and removal subsequently passed on Ubuntu 22.04 with two CPUs and 3 GiB memory.
The [scoped acceptance](../../docs/evidence/alpha-online-linux-hosted-20260908.md)
binds the downloaded package hash, root-owned protected input, actual APT path
and dpkg installed/removal transaction. It preserves the original diagnostic
harness's missing process observation; its planned post-online native check did
not run. Existing CPU/CUDA acceptance covers the identical offline package.
The published online script itself was also downloaded anonymously and hashed.
