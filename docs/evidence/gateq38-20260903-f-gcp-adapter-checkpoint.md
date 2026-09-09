# Gate Q3.8 GCP adapter checkpoint — 2026-09-03

Status: **PARTIAL / USD 0**

This checkpoint adds the source-bound GCP adapter for the durable Qwen3.8
complete-route controller. It deliberately does **not** authorize or execute a paid
route. The checked-in readiness ledger still contains no exact Q3.8 reservation, and
the adapter rejects both `start_route` and `collect_route` before authentication,
inventory, SSH, or any provider mutation because the protected Qwen3.8 host runtime
and status/evidence transport are not yet plan-bound.

Base source commit:

- `22b544b1b3b36c2c7997234e800ed67e53816fb1`

## What this checkpoint proves

The controller source boundary now includes
`scripts/gateq38_gcp_adapter.py`. A plan whose adapter bytes, controller bytes,
worker plan, ledger binding, or other required source changes cannot be loaded as the
same execution plan.

The adapter compiles, without executing, an exact eleven-command start specification:

- five image-backed 50 GB `pd-balanced` disks;
- one internal route firewall;
- four `g2-standard-8` / L4 workers and one `e2-standard-2` bootstrap;
- the pinned public deep-learning image and its explicit image project;
- private IPv4-only interfaces, no external IPv4 or IPv6, no service account, and no
  IP forwarding;
- one digest-derived network tag unique to the run and plan rather than a tag shared
  by every Q3.8 attempt;
- standard provisioning, restart-on-failure, terminate-on-maintenance, an 11-hour
  maximum lifetime, and automatic instance deletion;
- boot-disk auto-deletion for the unattended maximum-lifetime path. Controlled
  cleanup retains disks while deleting instances, then validates and deletes each
  disk explicitly.

Plan resource names must satisfy GCE's RFC1035 constraints and remain under the exact
run prefix. Read-only inventory enumerates that prefix for instances, disks, and the
firewall, rejecting missing, extra, foreign, malformed, publicly reachable,
privileged, deletion-protected, or shape-changed resources. The protected
`communityai-bootstrap-1` instance is observed only for its running health invariant
and is never included in compiled or cleanup mutations.

Until the host runtime exists, observation accepts only the canonical all-absent host
status. A static file therefore cannot promote a provider instance to `ready` or a
route to `passed`. A future runtime checkpoint must replace that blank-only rule
with fresh, protected, instance-generation-bound status and exact evidence transfer.

Cleanup dispatches directly from an exact source-bound controller decision rather
than requiring aggregate inventory to pass first. Every deletion re-describes and
validates the immutable resource binding independently, cleanup continues after
foreign resources and transient failures, terminal `TERMINATED` instances and
`FAILED` disks remain deletable, and a strict final inventory must prove exact
absence while the protected bootstrap remains running. A failed cleanup remains
retryable; it cannot be reported as complete while any run-scoped resource survives.

## Verification

All executable checks ran locally on Windows with injected provider responses. They
made no real GCP, Fly.io, or GitHub provider call and created no cloud resource:

- `137 passed` in
  `tests/test_gateq38_gcp_adapter.py tests/test_gateq38_route_controller.py`;
- `168 passed` across the adapter/controller plus the adjacent Gate 14 GCP
  executor, Gate 13 GCP provider, and multi-machine qualification tests;
- `1,686 passed, 10 skipped` in the repository offline unit matrix;
- Black, isort, Python compilation, and Git whitespace checks passed;
- independent adversarial review reproduced the 137 focused tests and returned PASS
  on the frozen source hashes.

The offline matrix uses the same exclusions as the preceding complete-route
controller checkpoint: tests that require an externally provisioned
`INITIAL_PEERS` swarm and unavailable optional bitsandbytes/PEFT runtime probes.

Canonical staged source bindings (SHA-256 over the Git-index blob bytes):

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_gcp_adapter.py` | 39,785 | `820d62a9a9d6c737d576fa659e3bbcdfb090f0f534494af6b8dab699c3d8db8b` |
| `scripts/gateq38_route_controller.py` | 88,955 | `bd972409e2c7932edad04c7bd452cc04db32347df650e5a82b47358ea167e7cb` |
| `tests/test_gateq38_gcp_adapter.py` | 25,877 | `14b37602ea24fdbb454642a1a3b108a83eba0b4a220485a103a4a77959cae740` |
| `tests/test_gateq38_route_controller.py` | 71,863 | `9adb7d33aff86345eb90f2cda0a735e68e4164fd0d4a14d2708a3bc32bcd8985` |

## Explicitly not proved

This checkpoint does not prove a Q3.8 reservation, four-GPU availability, live
provider creation, host bootstrap, protected host status, evidence collection, a
complete 64-block route, stock parity, same-session recovery, packaged cold
acquisition/cache reuse, or RTX 30/40/50 qualification.

The next unblocked no-spend gate is the exact source-bound Qwen3.8 host
runtime/staging package and protected instance-generation-bound status/evidence
transport. Only after that gate, an exact readiness reservation, and a fresh native
four-GPU capacity/pricing proof fit the remaining USD 44 exposure may a paid route
start be considered.
