# Gate 16: bounded alpha canary

Gate 16 is **open**. The September 7 local preflight establishes useful safety
prerequisites; it does not establish a monitored public canary. Use the qualified
installers and exact manifest/catalog identities from
[release readiness](RELEASE_READINESS.md). Signing is owner-deferred after alpha.

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
| Admission | Hold one first-message-idle inference stream from a canary peer, then attempt one additional stream from the same peer. Repeat once only after the refill interval. | The second stream is rejected before cache allocation, the first expires within effective step/session timeout plus 5 seconds, active-session counters return to baseline, and one valid subsequent request succeeds. |
| Malformed input | Send at most one request per invalid case to one explicitly allowlisted worker: oversized inference metadata, mismatched manifest, non-dictionary metadata, non-finite allocation timeout, invalid maximum length, and disabled training RPC. Wait at least the per-peer refill period between attempts. | All invalid requests are rejected within the effective request deadline; no worker restart or cache growth; counters remain bounded; rejection logging is bounded and excludes raw payloads. The local test implementations in `test_server_admission.py` are the reference cases. This live injector remains to be connected to the canary route. |
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
