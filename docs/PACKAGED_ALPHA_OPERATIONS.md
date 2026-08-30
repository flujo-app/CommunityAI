# Packaged public-alpha operations

This runbook defines the Gate 13 clean-host lifecycle for the unsigned CommunityAI
public-alpha packages. It applies to Windows and Linux. It does not apply to macOS,
does not test credits, and does not authorize cloud creation.

Passing the controller tests in this repository does **not** pass Gate 13. Gate 13
requires one complete real packaged lifecycle on each supported platform against the
published signed bootstrap and a live product route.

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

The controller is a standard-library qualification tool. It may be copied separately
to the host, but it does not install or import CommunityAI source. The product runtime
must consist only of the unpacked release executables. A source checkout, editable
install, repository PYTHONPATH, developer virtual environment, or invocation of
python -m drift invalidates the run.

## Evidence contract

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

### Linux

Use the archive utility appropriate to the published artifact and sha256sum. Reject
absolute or escaping archive members before extraction. Invoke the packaged GUI and
the frozen node executable directly, not a repository module.

Use the packaged keyring backend and the ordinary user's Secret Service session.
Evidence records only product-owned item counts. Do not fall back to plaintext
credential files merely to make the clean-host run pass.

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

## Publication checklist

A Gate 13 evidence record is publishable only when:

- the controller returns result=passed;
- the source commit and package/catalog/manifest identities match the release inputs;
- all sixteen phases ran on a real clean host;
- the report passes a manual privacy review;
- final cleanup is complete; and
- the corresponding Windows or Linux package remains available by its published
  checksum.

Archive the Windows and Linux records separately, then aggregate their bounded facts in
release readiness. Do not mark Gate 13 passed from controller unit tests, build-job
smokes, or only one supported platform.
