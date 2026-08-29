# Recovery test runbook

This runbook captures the Gate 7 lessons and defines the only supported shape for a
future real-model recovery test. Its purpose is to prevent model qualification,
artifact transport, provider control, and product recovery from becoming one repeated
test loop.

## What recovery testing proves

A recovery test proves one mechanism:

1. A complete route exists with redundant workers.
2. One selected contributor is hard-killed during an active request.
3. The client stops using that peer, selects the redundant route, and replays any
   required cached activations.
4. The request completes on the same inference session.
5. Every temporary resource is removed and every temporary credential is revoked.

Gate 7 proved this once with TinyLlama on five Fly Machines. Adding another catalog
model does **not** require repeating the provider recovery gate, the four-profile
platform matrix, stock parity, packaging smoke, and artifact checks together. Model
admission, platform qualification, provider recovery, and product flow are separate
contracts.

## Non-negotiable architecture

- Keep the worker runtime image model-agnostic and reasonably small.
- Supply the model ID, pinned revision, manifest digest, and assigned block range as
  runtime configuration.
- Download or mount verified model artifacts through the product's bounded artifact
  path and cache them independently of the container image.
- Never solve model delivery by baking every model's weights into a new Docker image.
  This does not scale to larger models and turns recovery testing into registry testing.
- A 100B model must use the same runtime image and external artifact/cache mechanism;
  it must not produce a model-sized Docker image.
- Build a small reusable controller image if a standalone qualification controller is
  still needed. It may contain `flyctl`, WireGuard tools, and the test scripts, but no
  model weights.

## Required prerequisites

Do not start a paid recovery run unless all of these are already true:

- The generic runtime image already exists at an immutable provider-readable digest.
- The selected model already has a signed manifest, exact artifact digests, and a
  published resource envelope produced under the
  [Gate 9 edge-envelope runbook](EDGE_RESOURCE_ENVELOPE_RUNBOOK.md).
- Artifact acquisition uses the same path the product will use. No ad hoc registry
  mirror or qualification-only model image may be introduced during the run.
- Automatic placement and the production signed catalog exist before the final
  product-realistic test.
- The exact provider app, region, Machine count, sizes, maximum duration, cost ceiling,
  run ID, credential lifetime, and cleanup command are written down before creation.
- No unrelated multi-gigabyte upload, image build, or provider migration runs in
  parallel with the control-plane test.

If a prerequisite is missing, return to the gate that owns it. Do not work around it
inside the recovery run.

## One-run procedure

For the provider-mechanism fixture:

1. Create one bootstrap and four workers under one unique run ID.
2. Give every block exactly two workers and record both disjoint routes.
3. Wait for one complete route and run one deterministic request.
4. Select a worker on the active route and issue one exact SIGKILL.
5. Observe peer loss, replacement selection, activation replay, same-session
   completion, and the expected deterministic output.
6. Destroy the five exact Machine IDs, confirm the run-tag query is empty, and revoke
   the run token.
7. Write one report. Pass or fail, stop there.

For the later product-realistic test, use the CommunityAI application rather than the
qualification controller:

1. Install a clean CommunityAI package.
2. Load the production signed catalog and bootstrap configuration.
3. Opt into contribution through the product UI or authenticated local control API.
4. Let automatic placement select the model and block range; do not inject a manual
   assignment.
5. Complete a normal user inference request.
6. Kill one contributing process or provider Machine by its exact identity.
7. Observe automatic replacement/recovery and complete another normal request.
8. Disable contribution or uninstall, then prove product and provider cleanup.

That product test runs once against one catalog-selected representative model. It does
not rerun Gates 5 and 6 for every model.

## Fly-specific lessons

### Registry authentication is separate

Fly login, Fly Machines API authorization, Fly registry authorization, and GHCR
authorization are different credentials. A package being labeled public in GHCR does
not guarantee that Fly's anonymous pull or remote builder can obtain its bearer token.

Gate 7 observed both direct Machine creation and a Fly remote build fail with GHCR
authentication errors. Treat that as a deterministic artifact-delivery failure. Do not
start a GCP-to-Fly mirror loop.

The preferred solutions, in order, are:

1. Use the model-agnostic runtime already stored in `registry.fly.io`.
2. Let the runtime fetch pinned, digest-verified model artifacts through the production
   artifact path.
3. If a container must be pushed, use an app-scoped credential whose lifetime exceeds
   the bounded transfer and keep layers small.

Do not push multi-gigabyte model-weight layers from a workstation. Docker silently
restarts failed layer uploads and can spend hours repeating the same transfer. The
first `Retrying in ...` message for a large layer is a stop condition, not permission
for an unattended loop.

### Keep data and control planes separate

- Inference and DHT traffic belong on Fly's private 6PN network.
- Stop, destroy, token, and inventory operations belong in a stable host/orchestrator
  control plane.
- The successful Gate 7 run used direct `flyctl` stop/destroy commands bound to the
  exact private-state Machine IDs. Raw controller-internal HTTP control calls had
  succeeded during provisioning but failed after the controller had been idle during
  worker startup and inference.
- Do not copy the disposable Gate 7 bridge into production. Implement the same
  separation with the maintained provider adapter or official client and retain exact
  identity/run binding.

### Identity readiness is asynchronous

`fly machine exec` can exit successfully before the node identity marker is readable.
The adapter must poll under one outer deadline. Identical duplicated markers are safe;
missing markers remain not-ready; conflicting markers are an error. Gate 7 added and
tested this behavior.

## Stop conditions and time bounds

Before the run, derive stage deadlines from the published resource envelope. The run
must have one outer deadline and provider deletion backstop. Stop and retain failure
evidence when any of these occurs:

- immutable runtime image cannot be pulled;
- model artifacts cannot be acquired through the production path;
- the first large Docker layer starts transport retries;
- the provider cannot create the exact five-Machine topology;
- identity or route readiness misses its bounded deadline;
- the exact SIGKILL cannot be acknowledged;
- recovery misses its bounded deadline;
- cleanup cannot prove an empty run-tag inventory.

A failed run does not automatically start another run. Diagnose the single owning
layer, make the smallest fix, and require an explicit new run decision. Never add a
new model, registry, cloud, builder, mirror, or qualification stage as a fallback inside
an active recovery attempt.

## Cleanup and local reuse

- Run disposable controller containers with `--rm`.
- Keep reusable tagged runtime/controller images.
- Use a dedicated named builder for qualification-only builds and remove its exact
  container, state volume, and cache after the gate.
- Never prune project volumes as part of test cleanup.
- Destroy provider resources by exact IDs first, then confirm the unique run tag is
  empty.
- Revoke the exact temporary token even when provisioning or recovery fails.
- Record cleanup in the same evidence document as the recovery outcome.

The Gate 7 cleanup retained reusable images and project volumes while removing the
orphaned Gate builder and unused build cache, reclaiming approximately 56 GB locally.
