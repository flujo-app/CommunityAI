# Gate 13 one-click GCP replay

Run Gate 13 GCP.cmd is the human entry point for the complete GCP replay.
It accepts no arguments and asks no questions while it is running.

Double-click Run Gate 13 GCP.cmd in Explorer, or run this one command from
the repository root:

    & '.\Run Gate 13 GCP.cmd'

The command does all of the following:

1. fails closed before provisioning unless GitHub, GCP, the network, images,
   protected bootstrap, L4 quota, and exact target absence pass;
2. resolves or builds the exact Windows and Linux production artifacts from
   the pushed branch HEAD, downloads their audit provenance, and binds both
   the GitHub wrapper and inner archive hashes and byte counts;
3. creates the run-scoped GCP L4 route plus the DHT, IAP, and private relay
   firewall rules used by the successful `gate13-20260901-a` run;
4. installs the exact retained route wheel, signed catalog, setup script, and
   helper commits used by that run—no route wheel is rebuilt—and executes the
   separately staged final route fence;
5. repeats the successful artifact path for each platform: the route downloads
   the multi-gigabyte GitHub wrapper with `curl`, verifies wrapper and inner
   archive hash and size, and exposes it on the private network at port 38081;
6. creates the clean client with the original startup script, which downloads
   that wrapper from the private route relay, verifies the inner archive, and
   installs the product; the relay is removed once the client is ready;
7. runs the Windows/Qwen qualification from an ordinary interactive user and
   deletes its VM and disk;
8. repeats the same fence, relay, client, qualification, and deletion sequence
   for Linux/Gemma from an ordinary desktop user;
9. deletes and proves absence of every run-scoped VM, disk, and all three
   firewalls; and
10. writes one terminal `result.json`.

The human does not copy URLs, issue SSH commands, click through the desktop
flow, or repair a run in flight. A failure still drives exact cleanup. If the
launcher process or computer is interrupted, the next double-click recovers
the prior run ID using that run's immutable provider-config snapshot, cleans
and verifies it, and only then begins a new run.
Provider maximum lifetimes remain a final backstop: six hours for a client and
sixteen hours for the route.

## One-time machine prerequisites

The Windows machine needs Python 3, Git, GitHub CLI, and Google Cloud CLI. The
exact successful-run route wheel must remain at the path pinned in
`config/gate13_gcp.json`; the launcher verifies its 389,107-byte size and
`7a42803811289e14f69835331e0fbab69dd353c70c835131c10bdfa96ca5f111`
hash before provisioning anything.
The flujo-app/CommunityAI GitHub account and the GCP account must already be
authenticated, and the current named branch HEAD must be pushed to origin.
Authentication is deliberately outside the replay because browser login is
interactive; the replay never prompts for or stores credentials.

If GCP authentication has expired, refresh it once before double-clicking:

    gcloud auth login

GitHub authentication, reusable GCP authentication, and the pushed-HEAD check
currently pass. The preflight stops before cloud mutation if any of those
checks later fail.

## Expected duration

The last successful automated host jobs took:

- Windows: 344 seconds (5 minutes 44 seconds)
- Linux: 294 seconds (4 minutes 54 seconds)
- Both qualification jobs: 638 seconds (10 minutes 38 seconds)

Those are the jobs themselves, after their clean VMs and route were ready.
The last successful final cloud window—from starting the Windows job through
Linux completion and exact cleanup—was about 4,081 seconds (1 hour 8 minutes).

For this full one-click command, reserve about 90 minutes when matching
production artifacts already exist. If it must build fresh GitHub artifacts,
reserve about 2.5 hours. Network/model download variance can extend either
estimate; the command prints ongoing phase updates.

## Result and privacy boundary

Each run writes ignored local state under:

    .gate13-runs/gcp/<run-id>/

result.json is the single pass/fail record. The two bounded client evidence
files are retained beside it. The command journal retains action names,
durations, and exit codes only. It does not retain command arguments, command
output, GitHub tokens, signed URLs, prompts, or model responses. Exactly as in
the successful run, a signed package URL is temporarily placed in the route
VM's `artifact-probe-url` metadata so the route can perform the download. It is
removed immediately after that download attempt, including on failure, and is
not retained in the result or command journal.

The implementation is separated into:

- gate13_cloud_orchestrator.py: provider-neutral lifecycle and cleanup order;
- gate13_gcp_provider.py: GCP provisioning plus GitHub artifact adapter;
- run_gate13_gcp.py: zero-input launcher, lock, recovery, and result display.

That boundary is the seam for the Azure adapter and, later, the deployment
tool. Route creation, client creation, client preparation, job execution,
resource deletion, and cleanup verification are already separate provider
operations.
