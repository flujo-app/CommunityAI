# Desktop block health and local download progress

Date: 2026-09-07. Source implementation and bounded tests passed. This is not
Gate 14 completion, installer qualification or a real Qwen swarm result.

## Delivered

- Model cards show numbered, keyboard-accessible block cells. Colors distinguish
  online replicas, joining announcements, unexpired signed reservations, offline
  announcements/local process failures, missing coverage and unknown/stale views.
  Clicking a cell exposes its counts and observed owners. Serving replicas retain
  precedence over failed local workers because another peer can cover the block.
- Peer tables show public name or abbreviated PeerID, observed/announced block
  ranges, reserved block counts, runtime version/dtype/quantization and relay use
  where reported. They also include this computer's configured workers. Remote
  download percentages and unused capacity are not inferred.
- Discovery verifies existing intent signatures, exact manifest binding, signer
  identity, revocation, range and expiration before displaying reservations. They
  never count as serving coverage. Expired reservations disappear at read time.
- Client and contribution-worker acquisition report current file size/progress,
  five-second transfer speed, cached/received and verified totals, retries and
  resumed bytes. Hash verification and atomic promotion remain authoritative.
  Totals cover selected files encountered so far; the UI does not invent a fixed
  whole-model total before shard selection. Download/loading/ready remain distinct.
- Supervised worker reporting uses a fresh directory per launch, bounded reads,
  allowlisted public fields, atomic throttled writes and shutdown cleanup. Windows
  venv launcher and child PIDs are handled by per-launch directory ownership.
  Missing display storage does not prevent a worker starting.
- Display refresh preserves selected block and expanded peer tables. The actual
  Qt window was rendered and inspected with explicitly synthetic preview data.

## Validation

- 265 related tests passed, two platform-specific skips: node, model manager,
  verifier/ranged downloader, discovery, worker supervision and all desktop tests.
- Live loopback HTTP tests start from a retained partial, force HTTP 429 retry,
  observe progress while transfer is blocked, then verify correct completion or
  corrupt-data rejection. Reloading the good cache makes no new HTTP requests.
- A real supervised Python process reports download progress, becomes paused on
  Pause, and its temporary progress directory is removed on shutdown.
- Signed-intent tests reject wrong-key/malformed records and remove expired
  reservations; Qt tests distinguish serving/joining/reserved/offline/unknown,
  retain expansion, render names as text, and keep downloaded bytes unverified.

The earlier source `5d9eec9` passed both Windows/Linux production engineering
package builds and all CI checks. These display changes need fresh package builds
and real packaged Qwen/resource/lifecycle observations. No cloud qualification,
code-signing enrollment, Store submission or public release occurred here.
