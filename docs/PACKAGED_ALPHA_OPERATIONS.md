# Packaged public-alpha operations

This runbook defines the Gate 13 clean-host lifecycle for the unsigned CommunityAI
public-alpha packages. It applies to Windows and Linux. It does not apply to macOS,
does not test credits, and does not authorize cloud creation.

Passing the controller tests in this repository does **not** constitute a fresh live
qualification. A replay requires the exact packaged desktop on each supported platform
against the published signed bootstrap and a live product route.

Gate 13 and Gate 15 are now separate release gates. Gate 13 covers verified package
startup, real-window inference, sharing-policy editing, Start, full application restart,
automatic sharing resume, Pause, and post-restart inference. Manual replacement,
retain/delete uninstall choices, and retained-data reinstall are Gate 15. The older
16-phase contract later in this document remains a useful combined Gate 13/15 release
exercise; it is not the shortest Gate 13 replay.

## Release boundary

The alpha artifacts are checksum-verifiable engineering releases. They are not
publisher-signed installers and do not contain an authenticated automatic updater.
Operators must make that limitation visible before installation and must not describe
the packages as signed, notarized, automatically updated, or rollback-protected.

A qualifying package must contain:

- the frozen desktop executable and separately frozen node runtime;
- the signed public-alpha bootstrap/catalog bundle;
- SHA256SUMS, provenance.json, and release-metadata.json;
- an explicit unsigned-alpha and incomplete-qualification warning; and
- no model weights.

The signed catalog is the model-policy trust root. Exact model manifests authorize
immutable upstream files, and their SHA-256 values authorize the bytes. The package,
catalog, and model transport must remain separate.

## Required inputs

Resolve these before touching a clean host:

1. The exact Windows and Linux artifacts from one successful production desktop
   workflow.
2. Each archive's published SHA-256 and byte size.
3. The full source commit recorded by its provenance.
4. The bundled catalog ID, sequence, catalog digest, and bootstrap digest.
5. The automatically selected model ID and raw manifest digest.
6. The Gate 9 selected whole-shard byte estimate for that manifest and platform.
7. One privacy-safe run ID for each host.
8. A copy of
   [gate13_packaged_lifecycle.py](../scripts/gate13_packaged_lifecycle.py).
9. A copy of
   [gate13_automated_playthrough.py](../scripts/gate13_automated_playthrough.py)
   when replaying the current Gate 13 boundary.

The controller is a standard-library qualification tool. It may be copied separately
to the host, but it does not install or import CommunityAI source. The product runtime
must consist only of the unpacked release executables. A source checkout, editable
install, repository PYTHONPATH, developer virtual environment, or invocation of
python -m drift invalidates the run.

## Current automated Gate 13 replay

The production desktop contains a hidden qualification mode that drives the real Qt
window. It does not call the controller in place of UI actions. The first process opens
the normal window, verifies the exact selected route, performs one localhost inference,
opens and saves **Edit sharing limits**, clicks **Start sharing**, and observes the
selected worker running. The process then exits normally so the desktop-owned node is
stopped. A second fresh desktop process proves sharing resumed after restart, clicks
**Pause sharing**, proves the worker stopped, and performs another localhost inference.

Each inference creates one in-memory temporary client key, retains only completion and
token counts, revokes the key, and proves the active-key baseline was restored. Session
timeouts are bounded to one hour each. The outer runner verifies the production archive
digest and byte size, runs the four packaged self-tests, executes both window sessions,
validates their strict privacy-safe evidence, and removes its exact run-scoped temporary
root.

Prepare one absolute-path config beside the staged runner. `work_root` must not exist and
its leaf must be exactly `.gate13-playthrough-<run_id>`:

~~~json
{
  "schema_version": 1,
  "run_id": "gate13-replay-a",
  "platform": "windows",
  "source_commit": "<40 lowercase hex>",
  "package_archive": "<absolute verified production archive path>",
  "package_sha256": "sha256:<64 lowercase hex>",
  "package_bytes": 1,
  "desktop_executable": "<absolute unpacked CommunityAI executable path>",
  "work_root": "<absolute parent>/.gate13-playthrough-gate13-replay-a",
  "model_id": "Qwen3.5 2B",
  "manifest_digest": "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
  "total_blocks": 24,
  "policy": {
    "sharing_enabled": true,
    "allowed_models": ["Qwen3.5 2B"],
    "preferred_models": ["Qwen3.5 2B"],
    "denied_models": [],
    "max_disk_space": "20GiB",
    "max_vram": "8GiB",
    "max_bandwidth_mbps": 100.0,
    "max_power_watts": 250.0,
    "pause_timeout": 30.0,
    "schedule": null
  },
  "session_timeout_seconds": 3600,
  "inference_timeout_seconds": 600
}
~~~

Run it as the ordinary qualification user with tracing disabled:

~~~text
python gate13_automated_playthrough.py --config gate13-windows-run.json > gate13-windows-evidence.json
~~~

The replay is desktop automation, not a headless smoke test. On Windows, provision with
[gate13_windows_client_startup.ps1](../scripts/gate13_windows_client_startup.ps1), wait
for the ordinary `M` account to own a real console session, and let the privileged host
adapter register the bound task with `Interactive` logon and `Limited` run level. `S4U`,
service-session, and SSH-session launches are invalid because Qt may start without an
actual user desktop or access to that user's Credential Manager.

On Linux, provision with
[gate13_linux_client_startup.sh](../scripts/gate13_linux_client_startup.sh). It installs
the package's complete XCB runtime closure, starts a TCP-disabled Xvfb display, and
prepares the ordinary `gate13` account. The host adapter runs the replay inside a private
`dbus-run-session`, starts GNOME Keyring's Secret Service, and passes only the fixed
display, home, and runtime-directory values into the bounded service. Do not substitute
`QT_QPA_PLATFORM=offscreen`: the qualification requires two real X11 windows and the
same native credential session across restart.

Use `platform: linux` and the exact Linux executable/archive for Linux. The durable
Gate 13 host-job adapter accepts this Python entrypoint on both platforms, binds the
config and source commit, and validates the aggregate before collection. A cloud replay
still requires a fresh cost authorization, route acceptance, exact clean clients, and
provider cleanup; prior Gate 13 reservations must not be reused.

## Combined 16-phase Gate 13/15 evidence contract

Platform startup scripts perform product actions and write one local JSON phase result
after each action. After final cleanup they place the ordered phase objects in one
document and run:

~~~text
python gate13_packaged_lifecycle.py --input phase-results.json
~~~

The controller writes one canonical JSON object to standard output. Redirect that
object to the candidate evidence file. A malformed, incomplete, reordered, duplicated,
non-finite, inconsistent, or privacy-unsafe document produces only:

~~~json
{"failure_code":"invalid_evidence","result":"failed","schema_version":1}
~~~

Do not treat that generic failure as a passing or diagnostic record. Diagnose locally,
remove private logs, repeat from a clean boundary when safe, and archive only a bounded
privacy-reviewed report.

The phase document has this header:

| Field | Contract |
|---|---|
| schema_version | Exactly 1. |
| run_id | A 1-64 character opaque label. |
| platform | Exactly windows or linux. |
| source_commit | One full lowercase Git object ID. |
| package_version | The bounded release version label. |
| package_sha256 / package_bytes | Exact downloaded archive identity. |
| model_id / manifest_digest | Exact catalog-selected model identity. |
| phases | The exact ordered phase list below. |

Every phase requires passed=true and a finite, non-negative duration_seconds. Unknown
fields fail closed.

### Required phase order

| # | Phase | Minimum fact proved |
|---:|---|---|
| 1 | package_verification | Archive identity, checksum inventory, provenance, release metadata, unsigned warning, no automatic update, and zero bundled weight files/bytes. |
| 2 | clean_install | No prior product, persistent data, credential material, source checkout, or source import; one package is installed or unpacked. |
| 3 | packaged_self_tests | Frozen desktop, node, worker, and bundled bootstrap checks pass. |
| 4 | signed_bootstrap | Pinned catalog signature/bootstrap and selected manifest identity verify. |
| 5 | selected_bytes | Exact whole-shard count/bytes are displayed while the verified cache is empty and before transfer starts. |
| 6 | verified_acquisition | The same files arrive directly from the manifest's immutable upstream revision; every digest verifies with at most three resumptions. |
| 7 | localhost_inference | Automatic model selection completes through loopback; only counts are retained. |
| 8 | bounded_contribution | Explicit opt-in starts one automatically placed worker with at least four enforced resource-limit classes. |
| 9 | contribution_pause | Operator pause completes within 300 seconds and leaves no worker process. |
| 10 | restart_cache_reuse | Restarted packaged inference uses the exact verified cache with zero artifact transfer. |
| 11 | manual_replacement | A checksum/provenance-verified manual upgrade or same-version reinstall preserves cache and credential counts; no updater or signing claim is made. |
| 12 | recovery | A documented operator-recoverable fault is observed, recovery actions complete, cached bytes survive, and localhost inference returns. |
| 13 | uninstall_retain | Explicit retain choice removes product/processes while preserving verified data and credential counts. |
| 14 | retained_data_reinstall | Reinstall reuses both retained cache and credentials with zero artifact transfer. |
| 15 | uninstall_delete | Explicit delete choice removes product, persistent data, credentials, and processes. |
| 16 | process_cleanup | Product, persistent data, credentials, processes, and qualification temporaries are all absent. |

The exact field-level and cross-phase rules live in the controller. It binds the
current Qwen3.5 2B and Gemma 4 E2B IT identities to their published raw manifest
digests, immutable revisions, artifact counts, and selected bytes. Use its tests as the
schema reference; do not create a more permissive platform-specific evidence format.

## Privacy boundary

The committed report may retain only:

- booleans;
- bounded counts and byte sizes;
- finite timings;
- source, package, catalog, bootstrap, manifest, and artifact digests;
- bounded catalog/model identity labels; and
- cleanup results.

It must not retain prompts, generated text, token IDs, request/session IDs, raw
credentials, credential names, user names, filesystem locations, process IDs, peer
identities, addresses, private or public endpoints, command lines, environment
variables, provider output, stdout/stderr, exception text, or private host inventory.
Do not put those values in phase documents even when the controller would later omit a
phase from the summary.

The localhost inference adapter may inspect response content in memory only long enough
to establish that a nonempty completion exists. It records a completion count,
generated-token count, response_content_retained=false, and token_identifier_count=0.
It must not log or serialize the response.

Credential evidence is count-only. Never export Windows Credential Manager or Linux
Secret Service values. The retain/delete checks observe product-owned item counts
before and after the operator choice.

## Clean-host preparation

Use a newly created ordinary user profile with:

- no CommunityAI installation or process;
- no persistent CommunityAI data;
- no product credentials;
- no repository checkout, Git dependency, developer virtual environment, or source
  path injection; and
- enough disk/RAM for the published Gate 9 envelope plus the package.

Record only zero/nonzero counts. Host names, user profiles, device serials, network
addresses, and filesystem locations are private diagnostic data.

Do not preload a model cache. Do not copy weights from another machine, package, image,
registry, or qualification cache.

## Verify and install the package

Before unpacking, compare the archive's SHA-256 and byte size with the release record.
For a GitHub Actions artifact, stage the exact
[gate13_download_artifact.py](../scripts/gate13_download_artifact.py) helper and the
platform's bound JSON configuration beside one another. Resolve the one-time final
artifact URL only on the authenticated operator, deliver it to the helper through
standard input, and never place it in an argument, environment variable, transcript,
or evidence. The helper disables proxies and redirects, requires the exact HTTPS host
and outer wrapper bytes, accepts exactly one stored regular-file member, and atomically
publishes the inner install archive only after its exact release size and SHA-256 pass.
The clean host receives no GitHub token or persistent GitHub credential.

After unpacking:

1. Verify every SHA256SUMS entry and reject missing, changed, extra, unsafe-link, or
   case-colliding payloads.
2. Verify the provenance source commit, build workflow, platform, catalog identity, and
   package metadata.
3. Require the release metadata to say unsigned public alpha, no publisher signature,
   no authenticated update, Windows/Linux only, no credits, and incomplete release
   qualification.
4. Inventory known model-weight extensions and all unexpectedly large payloads. The
   weight file count and weight bytes must both be zero.
5. Run the packaged desktop self-test and the frozen node self-test from the unpacked
   bundle. Run the packaged worker self-test exposed by the frozen node.
6. Confirm the executed binaries resolve inside the unpacked product and do not load
   modules from a source checkout.

The archive is the installable alpha unit. Install means unpacking it into a new
product directory owned by the test user. Do not run a source installer.

### Windows

Use native PowerShell argument arrays and Get-FileHash -Algorithm SHA256; do not use
Git Bash/MSYS for multiaddrs or paths. Keep the archive, unpacked package, persistent
data, and temporary phase records in separate operator-controlled locations. Invoke
the packaged GUI and node/CommunityAI-Node.exe directly.

The desktop owns its node credential through Windows Credential Manager. Evidence
records only the number of product-owned entries. Do not enumerate values or place a
secret in an argument, environment variable, ordinary file, transcript, or evidence.

#### Native Windows packaged-lifecycle orchestrator

Use
[gate13_windows_packaged_lifecycle.ps1](../scripts/gate13_windows_packaged_lifecycle.ps1)
for the complete 16-phase Windows run. Stage it beside
gate13_windows_localhost_inference.ps1, gate13_packaged_lifecycle.py,
gate13-windows-run.json, payload/communityai-desktop-windows.zip, and the package
audit files under audit/. Run it from native Windows PowerShell 5.1 as an ordinary
user; the helper accepts no arguments and rejects an existing work root.

The run input binds the independently downloaded archive to its release record and
the intended public catalog selection:

~~~json
{
  "schema_version": 1,
  "run_id": "gate13-windows-qwen-a",
  "source_commit": "<40 lowercase hex>",
  "package_version": "2.3.0.dev2",
  "package_sha256": "<64 lowercase hex>",
  "package_bytes": 1,
  "model_id": "Qwen3.5 2B",
  "manifest_digest": "<64 lowercase hex>"
}
~~~

Do not derive these values from an unpacked or locally rebuilt package. The helper
cross-checks them against the archive, audit, provenance, packaged runtime version,
and live selected catalog profile. It rejects unsafe or case-colliding ZIP members,
including Windows device names and trailing-dot or trailing-space aliases.

With transcription and shell tracing disabled:

~~~text
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\gate13_windows_packaged_lifecycle.ps1 > gate13-windows-evidence.json
~~~

Every packaged process is created suspended, placed in a kill-on-close Job Object,
and only then resumed. Fixed commands are bounded; acquisition has a 3,600-second
ceiling and at most three resumptions. Pause and cleanup require the exact automatic
worker PID to leave the product job, and final success requires the whole Job Object
to be empty. Any timeout, forced graceful cleanup, retained credential, or unverifiable
process-tree exit fails the run with generic privacy-safe evidence.

### Linux

Use the archive utility appropriate to the published artifact and sha256sum. Reject
absolute or escaping archive members before extraction. Invoke the packaged GUI and
the frozen node executable directly, not a repository module.

Use the packaged keyring backend and the ordinary user's Secret Service session.
Evidence records only product-owned item counts. Do not fall back to plaintext
credential files merely to make the clean-host run pass.

#### Native Linux packaged-lifecycle orchestrator

Use
[gate13_linux_packaged_lifecycle.py](../scripts/gate13_linux_packaged_lifecycle.py)
for the complete 16-phase Linux run. Stage it with the localhost-inference adapter
and gate13_packaged_lifecycle.py; these qualification helpers use only the Python
standard library. Run as the ordinary qualification user inside one private
dbus-run-session with an unlocked Secret Service provider. The host must authorize
only the fixed, noninteractive sudo -n /usr/bin/systemd-run and
sudo -n /usr/bin/systemctl operations used by the helper.

The helper creates system services with --uid set to that ordinary user,
KillMode=control-group, and the private D-Bus address. The GUI's
NodeLifecycleSupervisor, node, contribution worker, and daemon therefore remain in
one cgroup even when a child creates a new POSIX session. Graceful phases signal only
the GUI service's main PID with SIGTERM and require that root to return and the entire
cgroup to become empty. A cgroup.kill or SIGKILL fallback fails a graceful phase.
Recovery deliberately kills the owned fault cgroup and then starts a new owned unit.
Final success requires every run-scoped unit to be empty and removed.

Supply one absolute, private config path. The work root must not exist and its leaf
must be exactly .gate13-linux-<run_id>. The config contains no credential:

~~~json
{
  "schema_version": 1,
  "run_id": "gate13-linux-qwen-a",
  "release_root": "/qualification/original",
  "replacement_release_root": "/qualification/replacement",
  "work_root": "/qualification/.gate13-linux-gate13-linux-qwen-a",
  "model_id": "Qwen3.5 2B",
  "package_version": "2.3.0.dev2",
  "package_sha256": "<64 lowercase hex>",
  "package_bytes": 1,
  "replacement_kind": "reinstall",
  "replacement_package_sha256": "<same 64 lowercase hex>",
  "replacement_package_bytes": 1,
  "max_disk_space": "20GiB",
  "max_vram": "8GiB",
  "max_bandwidth_mbps": 100.0,
  "max_power_watts": 250.0,
  "pause_timeout": 30.0
}
~~~

For replacement_kind reinstall, digest and bytes must equal the original archive.
For upgrade, the replacement digest must differ. Bind all sizes and digests to the
bytes downloaded from the release job; do not recompute a config from a different
local build.

With shell tracing disabled:

~~~text
dbus-run-session -- python3 gate13_linux_packaged_lifecycle.py \
  --config /qualification/gate13-linux-run.json > gate13-linux-evidence.json
~~~

The acquisition invocation is fixed to:

~~~text
CommunityAI-Node edge-acquire <installed-manifest> \
  --cache_dir <empty-persistent-cache> \
  --max_resumptions 3 \
  --require_direct_upstream
~~~

Before phase 6, the helper requires the raw acquisition record to bind every unique
selected artifact path, role, size, and SHA-256 to the exact threshold-signed
installed manifest. It also requires direct_upstream_transfer=true,
mirror_used=false, source_class_verified=true, transport_override_present=false,
exact selected cache_bytes_after and cache_growth_bytes, no prior cache bytes, no
more than three resumptions, and final digest verification. Missing any raw field
fails the run; a projected or hand-authored summary cannot substitute for the raw
record.

Run Qwen3.5 2B and Gemma 4 E2B IT as separate lifecycle documents with unique run
IDs and absent work roots. They may be scheduled independently on separate clean
hosts, but cold-cache, native credential, cgroup, and final deletion proofs must
remain isolated per model.


#### Native Linux localhost-inference adapter

Copy
[gate13_linux_localhost_inference.py](../scripts/gate13_linux_localhost_inference.py)
to the operator-controlled qualification directory alongside the lifecycle controller;
do not copy a checkout or install project dependencies. The clean host needs only
Python 3's standard library plus the operating-system `dbus-run-session`,
`secret-tool`, and an unlocked Secret Service provider.

Start and keep the packaged desktop/node and the adapter inside the same
`dbus-run-session`. Create at least one ordinary local API key through the packaged
product first: the node deliberately refuses to revoke its last active API key, so
this precondition guarantees that the adapter's temporary qualification key can be
revoked. With shell tracing disabled, run the following inside that existing private
D-Bus session after automatic selection and verified acquisition are ready:

~~~text
python3 gate13_linux_localhost_inference.py > localhost-inference.json
~~~

The adapter reads the `org.communityai.desktop` /
`local-node-control-v1` control item through a fixed `secret-tool` argument vector
and a captured pipe. It never accepts a token through argv or the environment and
never writes one to an ordinary file, transcript, response, or evidence. It disables
HTTP proxies and redirects, calls only the fixed loopback control and OpenAI paths,
creates one in-memory temporary API key, makes one bounded automatic-model request,
and unconditionally revokes that key.

A successful standard output object is the exact phase 7
`localhost_inference` record accepted by the lifecycle controller. The adapter
retains neither prompt nor response content and records no key, token, request, or
endpoint identifier. Its generic failure object is not a phase record and must never
be included as passing evidence. Treat any nonzero exit, missing cleanup proof, or
remaining active qualification key as a failed platform run.

## First start, acquisition, and inference

Start the desktop normally. On first start it must use the packaged bootstrap, verify
the signed catalog, install exact manifests, and create the node configuration without
source tooling.

Before any model byte transfer, capture the product's selected model, manifest digest,
whole-shard file count, and selected bytes. They must match the Gate 9 envelope for the
selected model. The verified cache count must still be zero and transfer_started must
be false.

Authorize transfer through the product. The acquisition phase must prove:

- the immutable manifest revision;
- selected count/bytes equal acquired count/bytes;
- one successful SHA-256 verification per selected artifact;
- direct upstream transfer, with no registry or mirror;
- at most three resumptions; and
- the verified persistent cache contains the exact selected bytes.

Create a local API key through the packaged product, make one bounded model=auto
request through the documented loopback API, and retain only the selected model
identity plus counts. Delete any local diagnostic response immediately.

## Contribution, pause, and restart

Contribution is opt-in. Enable the product's automatic contribution mode with the
published resource envelope as an upper bound. The product must automatically choose
the exact manifest and a contiguous block range. One worker maximum and at least the
storage, memory/VRAM, bandwidth, and power/schedule limit classes must be active.

Run one bounded acceptance action, then use the product pause control. Pause must finish
within five minutes and leave zero contribution workers or descendants.

Stop the desktop-owned node cleanly, prove its process tree is absent, and start the
same packaged application again. Repeat one localhost inference. The exact selected
artifact bytes before and after restart must match, and transferred artifact bytes
must be zero.

## Manual replacement

There is no automatic alpha update. Stop all product processes and independently
verify the replacement archive before replacing product files.

A reinstall uses the same package digest. An upgrade requires a different verified
package digest. The controller rejects a claimed upgrade whose digest did not change
and a claimed reinstall whose digest did.

Replace the product directory manually while leaving persistent data and native
credential entries untouched. Start the replacement, repeat packaged self-tests as
appropriate, and complete localhost inference. Cache byte and credential counts must
be unchanged. Do not claim publisher signing, authenticated update, downgrade
protection, or automatic rollback.

## Recovery

Exercise one bounded, documented operator recovery that does not destroy the verified
cache—for example, stop the application during startup and follow the documented
restart/reconnect procedure. Record only that a fault was observed, the bounded number
of operator recovery actions, restored verified artifact bytes, and successful
localhost inference.

Gate 13 recovery proves that the package instructions work. It does not replace the
separate-machine route recovery gate or the Gate 14 automatic-contribution hardware
check.

## Uninstall choices

The alpha uses manual uninstall.

### Retain data

1. Pause contribution and stop the entire owned process tree.
2. Remove only the unpacked product.
3. Explicitly choose to retain persistent CommunityAI data and native credential
   entries.
4. Prove product/process counts are zero while verified artifact bytes and credential
   counts are unchanged.
5. Reinstall the verified package and prove zero-transfer cache reuse, credential
   reuse, and localhost inference.

The persistent data root is normally ~/.drift; never put its expanded host-specific
location in evidence.

### Delete data

1. Pause contribution and stop the entire owned process tree.
2. Remove the unpacked product.
3. Explicitly choose deletion of persistent CommunityAI data.
4. Delete product-owned native credential entries through the platform credential
   service.
5. Prove product files, persistent files/bytes, credential entries, and processes are
   all zero.

Do not recursively delete a home directory or an unresolved environment-variable
target. Resolve and validate each exact product-owned target first.

## Stop conditions and cleanup

Fail the platform run immediately on any:

- archive, checksum, provenance, release-metadata, catalog, manifest, revision, or
  artifact mismatch;
- bundled model weight;
- source import or developer-file dependency;
- transfer beginning before the selected-byte disclosure;
- non-loopback local API use;
- automatic update or publisher-signing claim;
- unbounded or multiple contribution worker;
- cache growth during the zero-transfer restart or reinstall checks;
- credential value exposure;
- phase-order, schema, or privacy violation; or
- missing process, persistent-data, credential, or temporary-file cleanup proof.

Always finish exact product cleanup. A failed action is not permission to leave a
worker, node, desktop process, persistent test data, credential, or phase temporary
behind.

## Combined 16-phase publication checklist

A Gate 13 evidence record is publishable only when:

- the controller returns result=passed;
- the source commit and package/catalog/manifest identities match the release inputs;
- all sixteen phases ran on a real clean host;
- the report passes a manual privacy review;
- final cleanup is complete; and
- the corresponding Windows or Linux package remains available by its published
  checksum.

Archive the Windows and Linux records separately, then aggregate their bounded facts in
release readiness. For a current-scope Gate 13 replay, the automated aggregate replaces
the combined 16-phase record only for the open/infer/share/restart/resume/pause boundary;
Gate 15 still requires separate replacement and uninstall evidence. Do not claim a fresh
live qualification from controller tests, build-job smokes, or only one supported
platform.
