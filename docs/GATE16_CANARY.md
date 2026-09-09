# Gate 16: bounded alpha canary

Gate 16 is **open**. The September 7 local preflight establishes useful safety
prerequisites; it does not establish a monitored public canary. Use the qualified
installers and exact manifest/catalog identities from
[release readiness](RELEASE_READINESS.md). Signing is owner-deferred after alpha.

September 8 scope update: the owner deferred additional conversation/performance
measurements and frozen periodic catalog-update qualification after alpha. The
catalog withdrawal/restore phases below are retained as beta procedures, not
current alpha blockers. Existing real worker-loss, fallback, rejoin, formation
and shutdown evidence must be reused before planning further work. The owner has
asked what a new integrated run adds; Gate 16's final alpha scope is under review.
No combined public canary is claimed to have passed. This scope update takes
precedence over the original full-run acceptance wording below.

## Reproducible local prerequisites

Run these from the repository root in the maintained Python environment. They do
not require model downloads or a GPU. The source suite includes a real loopback
TLS p2pd rejection test and signed DHT announcement round-trip, alongside isolated
admission, identity, health, selection, catalog and privacy tests.

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD\desktop\src;$PWD\tests"
python -m pytest tests/test_server_admission.py tests/test_protocol_identity.py tests/test_protocol_identity_network.py tests/test_public_worker_health.py tests/test_discovery.py tests/test_route_health.py tests/test_measured_model_selection.py tests/test_catalog_refresh.py tests/test_route_metrics.py tests/test_automatic_placement_privacy.py tests/test_gate16_catalog_drill.py -q
```

The frozen node probe uses a **new** output directory, empty local cache, CPU-only
local configuration, no configured peers and no contribution worker. It tests HTTP
authentication separation, malformed input, unknown-model rejection, policy
revision conflicts, mode persistence and disposable-key revocation. It sends no
valid inference request. Its five-second HTTP deadlines are probe limits, not
evidence for stalled generation deadlines.

```powershell
python scripts/gate16_local_preflight.py --node .gate13-runs/gate14-release-windows-v2-output/CommunityAI/node/CommunityAI-Node.exe --expected-node-sha256 158d4b8940b5e322a951819abbb31631a6cb059647a73e7a313a8c7f6e21955a --manifest public-alpha/catalog-qwen-v2/manifests/e62b19ad7d0c6af3dabe730105aefd4cf067ddc50063ffa74c00bd94a29bd7d0.json --output .gate13-runs/gate16-local-new-run --port 18116
```

The node digest above identifies the already-qualified Windows runtime from
`76b6d84fc52342af4fd2315926b187aaa36b1378`. Replacing that artifact requires a fresh
verified digest; do not copy a digest from an unverified replacement. The output
directory retains private host-local logs and a sanitized `result.json`; only the
latter is suitable for evidence export. The runner stops only its owned process
and verifies descendant exit, then removes its three generated credential files.
Native desktop credentials are outside this headless probe.

The signed catalog drill uses the real Qwen catalog/manifests with an ephemeral
test signing key and in-memory fetcher. It withdraws the community model, rejects
a lower-sequence rollback, and restores known-good content at a higher sequence.
It checks the custom local cache/device/timeout and resource preferences survive.
No production signing key or mirror is involved.

## Bounded live RPC driver

`scripts/gate16_live_rpc.py` now connects the malformed/admission cases to the
real worker RPC protocol. It defaults to **local preflight with zero network
connections**. Run it beside an already-owned worker's current public-health
file, in its maintained Python environment. Record that worker's exact direct
TCP multiaddress, manifest, served block and effective launch settings in the
private route inventory first. The health file does not identify its peer, so
that pairing is an operator prerequisite, not something the driver can prove.

Copy `docs/gate16-rpc-policy.example.json` to the private run directory and adjust
it to the actual effective settings. The example is the maintained public-route
launcher's policy; it is not evidence of any desktop worker's configuration.
The probe requires global active capacity greater than one, a per-peer active
limit of one, training disabled, finite timeouts no greater than 60 seconds,
and enough idle time for token-bucket refill before the second stream.

Use these variables from the recorded owned-route inventory; do not substitute
a shared bootstrap address or an arbitrary worker. Each output directory must
be new. The second command is the explicit network action.

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD\tests"
$rpcArguments = @(
  'scripts/gate16_live_rpc.py',
  '--manifest', $ownedWorkerManifest,
  '--expected-manifest-digest', $ownedManifestDigest,
  '--worker-multiaddr', $ownedWorkerMultiaddr,
  '--worker-label', 'canary-worker-a',
  '--block', $ownedServedBlock,
  '--health', $ownedWorkerHealth,
  '--policy', $verifiedEffectivePolicy
)
python @rpcArguments --output .gate13-runs/gate16-rpc-preflight-new
python @rpcArguments --execute --output .gate13-runs/gate16-rpc-live-new
```

The hard envelope is one ephemeral TLS client, 20 RPC calls, 128 KiB of total
protobuf request payload, 600 seconds of operations and up to 80 seconds of
cleanup. It sends no valid inference tensor and loads no model. Relays, automatic
NAT discovery, port mapping and IPFS bootstrap are disabled. The runner checks
TLS and the exact responding peer, waits for token refill while the first idle
lease remains active, checks the second same-peer stream rejects, and observes
worker-side lease release before closing its own input producer. Each malformed
case must match its own rejection category; overload cannot count as success.
Cache-token capacity and bounded public-health counters must recover.

The runner terminates only its own descendant client processes and separately
checks worker sessions/pushes released. Cleanup failures produce a failed
`result.json` and a nonzero exit. Raw errors, peer addresses and identities stay
out of exported JSON; retain any console logs privately. A passed RPC result is
partial evidence: valid post-probe inference, global saturation, identity churn,
GUI disclosure, full route cleanup and the other phases below remain required.

## Isolated live catalog channel

`scripts/gate16_catalog_channel.py` prepares an isolated channel from the real
release manifests and observes its ordinary packaged consumer refresh. It never
publishes or provisions anything. Preparation generates a distinct ephemeral
trust root, pre-signs baseline/withdrawal/restore at sequences 1/2/3, and retains
no private signing key. All phases expire after two hours. It prepares new
private node state offline with local-only mode and sharing paused; it downloads
no model and starts no process.

```powershell
python scripts/gate16_catalog_channel.py prepare --release public-alpha/catalog-qwen-v2 --base-url $ownedCanaryHttpsBase --initial-peer $ownedBootstrapMultiaddr --run-id gate16-new --output .gate13-runs/gate16-channel-new
```

The HTTPS base must be an already-authorized public HTTPS path ending in `/`.
Publish **only** the generated `channel/` contents to that path using its existing
operator workflow. Keep `private-node/` local; it contains host-local configuration.
Start the qualified installed desktop against the prepared `private-node` state,
under its normal owning lifecycle and a unique native credential service/account.
Keep source or automated Qt work offscreen. The prepared custom trust root is
deliberately separate from the bundled production root; this tests subsequent
ordinary refresh, not first-install trust bootstrap. The desktop retains its
saved custom-root state when bundled-root migration is not authorized.

The observer only reads authenticated loopback status and local signed state.
It neither starts the desktop nor creates/deletes its native credential. Bind
`$canaryConfig`, `$canaryNodeUrl` and `$canaryCredentialService` to that exact
private desktop lifecycle. The default observation deadline is 420 seconds
(maximum 900), allowing the ordinary 300-second refresh interval.

```powershell
$channel = '.gate13-runs/gate16-channel-new'
$observeArguments = @(
  'scripts/gate16_catalog_channel.py', 'observe', '--bundle', $channel,
  '--node-config', $canaryConfig, '--node-url', $canaryNodeUrl,
  '--credential-service', $canaryCredentialService,
  '--credential-account', 'control', '--timeout', '420'
)
python @observeArguments --phase baseline --output .gate13-runs/gate16-catalog-baseline-new
python scripts/gate16_catalog_channel.py advance-local --bundle $channel --phase withdrawal
# Publish the updated channel/catalog.signed.json through the owned HTTPS workflow.
python @observeArguments --phase withdrawal --previous .gate13-runs/gate16-catalog-baseline-new/result.json --output .gate13-runs/gate16-catalog-withdrawal-new
python scripts/gate16_catalog_channel.py advance-local --bundle $channel --phase restore
# Publish the updated channel/catalog.signed.json through the same owned workflow.
python @observeArguments --phase restore --previous .gate13-runs/gate16-catalog-withdrawal-new/result.json --output .gate13-runs/gate16-catalog-restore-new
```

Local phase activation requires exactly the next sequence and verifies all signed
phase hashes. Observation requires the expected installed signed catalog, a
**running** node with a later start identity after each transition, and an
authenticated in-memory `config_revision` matching the exact saved config bytes.
Updating files alone, an unrelated restart serving the older configuration, or
a stopping node cannot pass. The existing API exposes no active catalog digest;
the evidence binds saved signed policy to the node's active configuration revision.
It checks removal/restoration of automatic community priority, preserved local
preferences, no loaded model and paused workers. It does not prove active-generation
drain, inference/cache retention, GUI health or route shutdown. The lifecycle
owner must stop the private desktop and remove its disposable native credential.

Both drivers have focused local tests, including the real handler over loopback
TLS without model allocation, false-pass regressions and cleanup-failure evidence:

```powershell
python -m pytest tests/test_gate16_live_rpc.py tests/test_gate16_catalog_channel.py -q
```

Actual live prerequisites still include an owned formed route and live health
access, the recorded effective worker policy, an owned HTTPS channel with its
publication workflow, and qualified installed consumers. None is created by these
drivers, and no public Gate 16 pass is claimed by the local tests.

## Public run envelope

Use an explicitly identified canary route and an allowlist of its exact worker
identities. Record the installer checksums, runtime commits, manifest digest,
signed catalog digest/sequence, client OS/hardware class, and route inventory before
starting. Keep public identities/addresses in a private operational record; export
opaque roles and aggregate counts. Existing historical cloud budgets do not
authorize a new paid route. Use already-authorized resources, or obtain a fresh
bounded budget before provisioning.

Allow 30 minutes after route formation, at most two ordinary desktop clients,
and at most two concurrent ordinary generations. Use fixed synthetic prompts,
at most 32 generated tokens per normal request, and at most 12 ordinary requests.
Reserve the last five minutes for shutdown and independent cleanup. Every operator
action must name an owned route/process/resource; do not stop a shared bootstrap.

Record the **effective** admission and timeout values on every worker. The
maintained public-route launcher currently fixes 8 active sessions, 1 per peer,
2 new sessions/second globally with burst 4, 0.25 per peer/second with burst 1,
512 tracked peers with 300-second expiry, and 4 pending pushes. It fixes request
and session timeouts at 60 seconds, step timeout at 30 seconds, batch size 1,
512 cache tokens and 16 MiB chunks. Desktop contribution launchers may use their
own finite settings; report those actual values rather than attributing the
public-route launcher's settings to them.

An explicit `request_timeout` is not an end-to-end generation deadline. Record
client retry count, backoff and local `local_max_seconds` separately. Cap each
canary action with an operator deadline and terminate only the exact test client
on expiry. A client-side timeout is a failed observation until worker counters
and owned process/cache release are independently checked.

## Required observations

| Phase | Bounded action | Required evidence |
| --- | --- | --- |
| Baseline | Open both actual installed clients. Make one local and one community request with the fixed synthetic prompt. | Local inference works; community route identity and complete coverage match the allowlist; generated-token count and latency are recorded without text. Sharing starts only after opt-in. |
| Disclosure | Read the Home and Sharing pages before enabling contribution. | Home states that computers helping may see submitted content; Sharing states that request content may be visible to the contributor or software on that machine. Do not present transport encryption as end-to-end inference confidentiality. |
| Admission | Use the live RPC driver to hold one first-message-idle inference stream, wait for token refill while its lease remains active, then attempt one additional stream from the same peer. | The second stream is rejected before cache allocation, the first expires within effective step/session timeout plus 5 seconds and active-session counters return to baseline. Separately prove one valid subsequent request succeeds. |
| Malformed input | Use the live RPC driver for one request per invalid case to one explicitly allowlisted worker: oversized inference metadata, mismatched manifest, non-dictionary metadata, non-finite allocation timeout, invalid maximum length, and disabled training RPC. Wait at least the per-peer refill period between attempts. | All invalid requests receive their specific rejection within the deadline; no worker restart or cache-capacity loss; counters remain bounded. Separately inspect bounded rejection logging for absence of raw payloads. The driver is implemented; an actual owned-route run is still required. |
| Health reconstruction | Pause one owned contributor through the real desktop, wait past its signed announcement/intent validity, then restart it. | Grid distinguishes serving coverage from joining/reserved blocks and local failures. When a unique span disappears, status becomes incomplete/unavailable and automatic selection falls back. On return, coverage reconstructs from fresh signed records; stale reservations do not count as service. Record time to disappearance and recovery. |
| Catalog withdrawal | In an isolated canary catalog channel, publish a correctly signed higher sequence retaining the approved local model/rung and withdrawing the community model/rung. Wait for an ordinary refresh and idle reconfiguration. | The consumer authenticates the new digest; automatic selection and contribution approval exclude the removed community digest. Local inference and custom settings/cache survive. Invalid signature, equivocation and lower sequence remain rejected. |
| Route disable | Pause/stop each exact canary worker, then select local-only in both canary clients. Do not rely on catalog withdrawal alone as an emergency stop. | Every owned worker tree exits; remote coverage ages out; automatic requests remain local; no local controller silently restarts the route. |
| Restore | Publish the previous known-good catalog contents under a **newer** sequence, restore only the recorded owned workers, and leave sharing disabled until deliberately resumed. | Fresh authenticated coverage is required again. No rollback guard is erased. A normal request succeeds within the stated deadline. |
| Cleanup | Exit canary clients, stop owned route processes/resources, remove disposable API/native credentials, and preserve settings/cache selected for retention. | Independent audit confirms exact resource/process absence, no remaining test credential, and retained-data sentinel/hash. Export only sanitized aggregates and evidence digests. |

Catalog withdrawal deliberately retains older exact model selectors as manual
choices. It changes automatic selection and contribution approval; it does not
remotely revoke a user's explicit model configuration. Local-only currently
governs automatic selection as well. Emergency route disable therefore requires
stopping the **actual canary workers**, and an explicit-selector request should be
observed failing/unavailable once that exact route is gone. Independent workers
outside the owned route cannot be stopped by this drill.

The consumer refresh interval defaults to 300 seconds and waits for active loads
and generations to finish before restarting. Record the observed delay; a public
catalog change is not an immediate kill switch. Keep the canary catalog separate
from the shared alpha channel until its withdrawal/restore sequence passes.

## Stop conditions and evidence

Stop the canary on any leaked credential/content, accepted malformed signed
record, unbounded queue/cache/session growth, repeated worker restarts, stuck
cleanup, unintended peer/route use, or an operator deadline. Preserve the failing
phase and sanitized reason; do not rerun over the same record and label it passed.
If an ordinary request is rejected by expected admission, record that as an
overload result and retry only once after the configured refill interval.

The final Gate 16 record must bind each phase to the qualified installer/runtime,
give wall-clock start/end and monotonic durations, record aggregate health before
and after, include outcome and cleanup for failed attempts, and list any omitted
observation. Gate 16 passes only after every required live phase and independent
cleanup pass. Local test success or a generated protocol cannot close it.

September 7 local evidence is in
[`gate16-20260907-local-preflight.json`](evidence/gate16-20260907-local-preflight.json).
