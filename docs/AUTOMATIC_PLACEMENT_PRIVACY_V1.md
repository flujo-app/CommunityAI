# Automatic placement privacy review v1

Status: reviewed for the bounded Windows/Linux public inference alpha on 2026-08-29.

This review covers the Gate 10 automatic-placement data path: completed-route
aggregation, signed route-demand observations, placement intent leases, replay
journals, catalog-authorized observer roots, placement status, and related logs. It
does not approve credits, payments, macOS, training, stable-service telemetry, or an
exhaustive hostile-network program.

## Decision

The implemented public coordination path passes the alpha data-minimization floor. It
does not publish prompts, generated text, raw token IDs, API keys, request or client
IDs, remote addresses, human operator names, local paths, or per-request histories.
The public records contain only content-addressed model identity, coarsened operational
measurements, bounded resource intent, cryptographic public identity, and short-lived
ordering metadata. Locally, the node necessarily loads private identity keys and retains
configured manifest, cache, and identity paths; those are inventoried separately below.

This is a privacy review, not an anonymity claim. A stable public observer or worker
key is linkable across its signed records, the selected manifest is public, and coarse
demand buckets reveal approximate recent activity for that manifest. Those disclosures
are necessary for authenticated public coordination and are bounded below.

## Data inventory and retention

| Stage | Data used or retained | Location and retention | Excluded data |
| --- | --- | --- | --- |
| Completed local route | Exact manifest digest, success bit, completion-token count, useful duration, last observation time | Process memory only; two fixed five-minute aggregate windows, at most 4,096 counted events per model/window | Prompt, output, token IDs, request/client identity, API key, address, error text, and individual event records |
| Local utility snapshot | Manifest digest plus bucketed attempts, successes, useful throughput, reliability, and age | Process memory; rejected beyond the planner bound of at least 90 seconds and otherwise three discovery periods | Raw timings, individual failures, request contents, and signer identity |
| Published route demand | RSA public key and fingerprint, signature, exact manifest digest, the eight-field coarse observation, issue/expiry time, and sequence | Untrusted DHT; maximum 90-second signed lifetime; remote stores use exclude_self | Prompt/output, route endpoint, request/client identity, local path, and exception detail |
| Placement intent | RSA public key/fingerprint and PeerID, exact manifest and block range, artifact bytes, block count, optional coarse throughput, nonce, signature, issue/expiry time, and sequence | Untrusted DHT; ten-minute lease in the automatic service; remote acknowledgement required with exclude_self | Artifact contents, hardware serials, local cache or identity path, IP address, prompt/output, and user identity |
| Replay protection | Record kind, public key fingerprint, issue time, sequence, record digest, and bounded retention deadline | Local per-manifest journal; 256 active scopes and 256 KiB maximum; logical entries expire after the record-kind replay horizon, are ignored after reload, and are removed from disk on the next successful journal rewrite | Public keys, signatures, payloads, prompts, outputs, addresses, nonces, errors, and private keys |
| Observer authorization | Sorted public RSA key fingerprints | Threshold-signed catalog and local node configuration; absent/empty disables remote demand | Observer private keys, operator names, organizations, and network locations |
| Local configuration and identities | Manifest/cache/worker identity paths, including a possible username; loaded worker/intent/observer private-key objects; planner jitter seed and lease cache derived from identity paths | Owner-controlled node configuration and process memory; keys live for the owning process or service object and private key files persist until the operator removes them | None of this row is published in intent/demand records or placement status |
| Placement status | Exact selected manifest/range, coverage and quantized demand buckets, bounded policy and placement reasons | Authenticated local control API | Observer fingerprints, prompts/outputs, API keys, private paths, and raw exception details |
| Local logs | Fixed expected observer-credential/publication failures; unexpected automatic-placement and discovery faults can retain local tracebacks | Local process logs under host/operator controls | Expected observer-key failures omit the exception and path; unexpected-fault traceback privacy is not guaranteed |

The replay journal is deliberately not a request log. Its entries may remain as expired
bytes until the next successful journal rewrite; restart drops them from memory but does
not guarantee physical removal. They contain public identity and ordering metadata only. The journal uses atomic replacement, a 256 KiB ceiling, and
best-effort owner-only mode. Secure deletion is not claimed because filesystem,
backup, and SSD behavior cannot guarantee it.

## Controls reviewed

- Route outcomes are aggregated at collection time. The tracker has no parameter or
  field in which to store prompt, output, request identity, address, token IDs, or an
  exception.
- Published observations use fixed count, throughput, reliability, and 15-second age
  buckets. A window must be closed and contain at least four completed routes.
- Signed record parsers reject missing, unknown, duplicate, non-canonical, non-finite,
  stale, future, replayed, equivocating, revoked, or manifest-mismatched data.
- Remote observations are accepted only from 2–32 sorted RSA roots covered by the
  threshold-signed catalog. Unlisted keys are discarded before signature and replay
  processing. An absent or empty root list disables the signal.
- Nodes never generate route-demand.key. A publisher key must be separately
  provisioned and match a signed root. Consumers do not need an observer private key.
- A hot-edited root-list mismatch disables both publication and consumption until
  restart, preventing observations from two trust epochs from being mixed.
- Local demand contributes at most six score points and remote demand at most two.
  Neither can override local policy, artifact verification, the signed-intent
  requirement, a 10-point migration margin, a 100-point replica step, or operator
  pause.
- Expected failure to load an observer credential emits a fixed warning without the
  exception or filesystem path. Expected DHT publication failures log only the public
  manifest digest. Unexpected automatic-placement/discovery faults retain diagnostic
  tracebacks and therefore remain local-log risk.

The executable contract in tests/test_automatic_placement_privacy.py fixes the allowed
in-memory aggregate, intent, demand, replay, and warning schemas. Existing strict
protocol tests cover unknown fields and bounded values.

## Threat assessment

Public-key linkability is accepted for the alpha because authentication, revocation,
and replay ordering require stable public identity. Observer rotation currently
requires a new signed catalog list and a node restart. The catalog deliberately holds
fingerprints rather than human-readable operator metadata.

A malicious authorized observer can withhold or lower its vote and reduce availability.
One high compromised observer cannot inflate a lower honest observation because two
authorized roots are required and the lower median is used. Colluding authorized
observers, compromised catalog signers, traffic analysis outside the application,
DHT flooding, host compromise, unexpected-fault tracebacks, local log access,
filesystem backups, and proof of real-world operator independence remain outside this
software-only review. They must be handled by key governance, operating procedures, admission controls, the bounded
canary, and later hostile-network testing.

## Verification evidence

The privacy slice adds three executable checks for aggregate-only memory, exact public
record/journal schemas, forbidden-field absence, and fixed path-free credential
warnings. It reuses the Sybil slice evidence that 30 fully valid unauthorized keys
cannot contribute a vote or replay scope, and that catalog omission safely disables
remote demand while preserving old signatures.

Local verification passed a 108-test focused matrix and a 258-pass, 2-skip expanded
catalog/node/API matrix. Independent review passed 108 tests with 1 skip and a
225-pass, 2-skip broader subset. Its adversarial probe covered every caught observer-key
load exception plus an unauthorized key and found no exception detail, path, or key ID
in warnings; prompt and identity-path injection into strict public schemas also failed
closed. Black, import smoke, import order, and diff checks pass.

Gate 10 remains open after this review. Bursty-demand convergence, herd resistance,
load behavior, and the bounded public canary still require separate evidence.
