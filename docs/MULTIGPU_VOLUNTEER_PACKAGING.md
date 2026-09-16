# Dedicated volunteer portable packaging

Date: 2026-09-16. Scope: source packaging support and local fixture validation.
No frozen binary, Ubuntu installation, physical multi-GPU inference or beta
release qualification is established by this change. New spending: $0.

The build command now accepts `--profile multigpu-volunteer`. The ordinary
`standard` default retains its existing name, channel and archive layout. The
volunteer profile selects these fixed source identities:

| Field | Volunteer value |
| --- | --- |
| Frozen launcher | `desktop/launch_volunteer.py`, calling `app.volunteer_main` |
| Frozen node launcher | `desktop/launch_volunteer_node.py`, enforcing the same fixed profile |
| Bundle directory and executable | `CommunityAI-MultiGPU-Test` |
| Package and Linux archive | `communityai-multigpu-test`, `communityai-multigpu-test-linux.tar.gz` |
| Metadata and provenance channel | `multigpu-volunteer` |
| Default output and work directories | `desktop/dist/multigpu-volunteer`, `desktop/build/multigpu-volunteer` |

The launcher forces the existing isolated runtime profile before argument
parsing. A command-line profile override is rejected. The runtime profile keeps
its own state, cache, credentials, local port and instance lock, starts sharing
paused, and disables login startup and the ordinary updater. This is application
state separation, not an OS sandbox or Protected execution.

Review found that a generic bundled node could retain ordinary defaults when
launched directly. The dedicated node launcher now supplies the fixed test
configuration, data directory, port, native credential identity, paused sharing
and CPU local inference flags even without GUI arguments. Conflicting, duplicate,
abbreviated and unsupported arguments are rejected. Bootstrap uses the same fixed
output paths and accepts one explicit trust input. Both paths apply the private
profile cache environment before importing the generic runtime.

Worker and acquisition modes require the actual parent PID and fixed profile root
inherited from a node started through this launcher. Paths must remain inside the
profile; user-selected configuration/code/token options and implicit `config.yml`
are rejected. Download progress output is stripped for direct node/bootstrap
and diagnostics, and constrained to the profile for supervised workers.
These checks prevent accidental independent worker invocation; the inherited
marker is not authentication against a malicious process owned by the same user.
Multiprocessing frozen-child dispatch still runs before profile handling. Exact
help and self-test actions remain available without starting a worker or model;
the explicit optional native CUDA diagnostic still requires hardware when used.

The volunteer build requires Linux and an explicit full `--source-commit`
matching clean release source inputs. Both dedicated launchers are included in the source
check. Source identity is checked again after packaging and smoke checks, before
provenance is written. Metadata, checksums, archive verification and desktop
metrics all receive the same explicit profile. Verification never selects a
profile from editable metadata; supply `--profile multigpu-volunteer` to verify
this artifact. Missing commit/tree identity and cross-profile claims are rejected.

Volunteer build/work destinations must be empty and unlinked. They cannot overlap
each other or ordinary default build/output paths in either direction. Preserve
partial outputs after a failure and choose fresh paths for a retry. PyInstaller's
cache is also placed in the dedicated work directory. Catalog data is included
only when explicitly supplied through `--publication-bundle`; the ordinary
desktop catalog is not picked up automatically for this profile.

Frozen diagnostic checks run with a temporary home, profile caches, temporary
directories and platform application directories. Known inherited Hugging Face
tokens are removed and Hugging Face offline mode is enabled. This avoids preparing
or inspecting the operator's real volunteer profile during a build. These smoke
checks use existing local fixture behavior; they do not run or qualify a model.

After the source is integrated and committed, a Linux operator can prepare the
portable test artifact using the existing pinned build environment:

```sh
python desktop/build_desktop.py --profile multigpu-volunteer --source-commit "$(git rev-parse HEAD)"
python desktop/build_desktop.py --profile multigpu-volunteer --verify-release-output desktop/dist/multigpu-volunteer
```

This command has not been run to produce a frozen binary in this checkpoint.
The current runtime normalizer requires PyTorch `2.6.0+cu124`; this change does
not establish XPU, MPS, MIG or other backend support. Any eventual volunteer
delivery still needs confirmed Ubuntu details, integrated/reviewed GPU controls
and aggregate limits, frozen sidecar qualification, exact model/backend profile,
checksum and source identity,
known limitations and stop instructions, then installed and real hardware tests.
The owner requires the finished Sharing interface to show all available GPUs,
with per-card VRAM and compute controls plus master controls affecting every
card. The reported eight-H100 volunteer host must be usable through that complete
interface; internal staged checks are the development team's work, not a request
for the volunteer to perform a one-card then two-card rollout.
No stable installer/update-feed change or public publication is included.

Final local validation passed **85 tests, zero skips and 110 warnings** across
packaging, runtime normalization, profile isolation, shell integration and the
dedicated node launcher. Fifteen source/test dependency hashes were unchanged
before and after the combined run. The suite exercises actual small archives,
fresh-process verification, source launcher self-tests, lifecycle argument tuples
and real parent/child context checks. Runtime/model work is stubbed in sidecar
dispatch fixtures; no frozen application or physical inference was executed.

Evidence is under `C:/Users/Moe/.communityai-beta/volunteer-packaging-*`: the final
log, JUnit XML, before/after hashes, validation result, three final review records
and atomic checkpoint packet. Review's direct-sidecar finding is resolved by the
new launcher. Leading-minus option values are rejected to keep downstream argument
boundaries unambiguous; review did not demonstrate a working exploit in the
earlier form. Internal review does not replace independent release security,
installed Linux, real hardware or full-beta acceptance evidence.
