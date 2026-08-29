# Automatic placement convergence and load acceptance v1

This report records the deterministic Gate 10 software acceptance boundary for the
public-alpha automatic contributor. It is evidence for the planner and node-configuration
contract, not a claim that production churn, regional partitions, or real hardware ceilings
have already been qualified.

## Enforced invariants

Automatic placement now enforces all of the following before worker artifact acquisition:

- one public-alpha `auto` worker per node configuration;
- no more than 32 automatic-placement model candidates;
- no more than 512 blocks in an automatic-placement candidate or worker request;
- a reconciliation period no shorter than one second;
- fresh exact-manifest replica coverage and the existing local policy/resource checks;
- a 15-minute minimum residency, five-minute cooldown, and 10-point migration margin; and
- a single-pass bounded contiguous-window scan.

For equal coverage, a stable hash of the node seed, manifest digest, and block range is the
final range rank. Model choice has a separate deterministic dispersion band in `[0, 32)`
points. The maximum non-coverage terms remain bounded below one replica step:

```text
preference 20 + priority 10 + local demand 6 + remote demand 2 + dispersion <32 < 100
```

A one-replica deficit therefore remains authoritative. The combined demand hint remains at
most eight points, below the 10-point migration margin. Intent leases are not consumed as
placement votes; allowing arbitrary worker identities to repel contributors would create a
cheap Sybil attack.

## Deterministic acceptance scenarios

`tests/test_automatic_placement_convergence.py` exercises the real planner rather than a
separate simulator.

1. A cold cohort of 512 distinct node seeds receives the same two-model, eight-block
   snapshot. Repeating the cohort produces identical decisions. The fixed fixture assigns
   332 nodes to the catalog primary and 180 to the standby; both models use every block.
   Primary block allocations range from 31 to 48 and standby allocations from 16 to 28, so
   no model receives 85% of the cohort and no range receives twice its model mean.
2. A 128-node committed cohort receives maximum valid local and remote demand for its other
   model. It performs zero migrations at 60 seconds and zero demand-only migrations after
   residency at 901 seconds.
3. Two 4,096-node fresh-arrival cohorts receive maximum local plus remote demand for one
   model. Maximum demand aligned with catalog priority selects 3,355 primary and 741 standby
   nodes; maximum standby demand selects 2,387 standby and 1,709 primary nodes. Neither fixed
   fixture reaches the 85% herd boundary.
4. Sixty-four planners receive a real one-replica deficit after residency and migrate.
   Reversing the deficit at 1,000 seconds causes zero immediate reversals; the reverse move
   is permitted only after the next residency/cooldown boundary at 1,802 seconds.
5. Four rolling 128-node arrival groups update coverage after every admitted placement.
   All 16 model/block cells remain populated, each model's block spread is at most one, and
   the static catalog priority remains below the 85% herd threshold. Resetting one standby
   block to zero makes the next two arrivals restore it to two replicas.
6. Limit and limit-plus-one cases cover candidates, blocks, automatic workers, and the
   reconciliation floor. A deterministic exhaustive small-window probe also compares the
   bounded scan's selected coverage tuple with the prior lexicographic optimum.

## Verification

The local focused planner/configuration matrix passes 78 tests. A broader
catalog/publication/bootstrap/protocol/route-metrics/discovery/planner/privacy/node/API
matrix passes 214 tests with 2 skips. The real Windows Hivemind signed-announcement round
trip exposed an earlier durable-replay regression: `ReplayGuard` contained a thread lock
that could not cross the DHT multiprocessing boundary. The guard now excludes and recreates
that lock during serialization and reloads a newer persistent journal watermark after
deserialization; the protocol plus real-DHT matrix passes 15 tests. The persistent-path
pickle/reload contract is also executable in that protocol suite.

Independent verification reproduced the 78-test focus, passed an expanded 235-test matrix
with 2 skips, and passed the 15-test protocol/real-DHT probe. It also exercised exact
residency, cooldown, switch-margin, score-envelope, 32-by-512 load, and 1,000-case
sliding-window equivalence boundaries.

Formatting, import order, import smoke, diff checks, and the bounded-window equivalence probe
pass. No GCP, Fly, registry, public route, credit, payment, or macOS state was touched, and
this slice spent USD 0.

## Residual scope

These deterministic tests establish cold-cohort dispersion, rolling convergence, bounded
demand influence, migration hysteresis, abrupt single-block repair, and finite local work.
They do not establish behavior under real packet loss, long partitions, regional correlated
failure, dishonest coverage providers, or hardware/resource envelopes. Gate 9 must still
publish the Windows/Linux Qwen/Gemma envelopes, and later packaged-flow and canary gates
must exercise the real installer, worker, and public-route lifecycle.
