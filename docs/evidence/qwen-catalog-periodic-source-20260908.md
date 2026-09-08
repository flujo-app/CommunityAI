# Signed periodic catalog refresh: source acceptance and frozen replay plan

This checkpoint connects the real periodic `CatalogRefreshService`,
`CatalogBootstrapInstaller` signature/rollback handling, and `ModelManager`
admission/lease implementation. Only the HTTPS document transport and wall-clock
input are substituted. Test catalogs have disposable signing keys; model
runtimes are inert objects. No network request, model weight, GPU load, desktop
window, Docker start or production signing key is used by these tests.

## Source observations

- A signed withdrawal is staged while two leases hold the withdrawn manifest's
  runtime. The original immutable catalog and active runtime remain usable;
  restart waits until both leases have been released.
- At the restart callback boundary, the real manager has already closed request
  admission. A manager configured from the accepted new catalog no longer
  approves contribution for the withdrawn manifest.
- A loader that has not returned also holds off restart. When it returns, its
  newly acquired lease continues to hold off restart until released.
- Periodic attempts with a tampered signature, rollback sequence or equivocation
  keep the accepted configuration and rollback guard unchanged, do not request
  restart, and leave request admission usable.
- Closing the refresh service while it waits for active work ends its thread
  without interrupting that lease or claiming a restart. The staged immutable
  update remains available for a later normal node startup.

These are source-level threading and state-transition observations. They do not
claim generated tokens, an actual server restart, authenticated HTTPS transport,
frozen-binary behavior or complete Gate Q3.8 acceptance. The regression results
and source hashes are recorded in the companion
[checkpoint record](qwen-catalog-periodic-source-20260908.json).

## Frozen Linux replay in two separate scopes

The startup-migration helper now supports ordinary Linux users and the packaged
`node/CommunityAI-Node` name. It keeps Qt offscreen by default, rejects root,
uses no Windows-only process flags, and treats zombies as non-executing while
preserving PID/creation-time checks and failure on unreadable ownership. It does
not claim a visible Linux window. Run it only after the current installer test
has stopped its desktop and node, inside its existing ordinary-user DBus and
unlocked native keyring session:

```sh
PYTHONPATH=/repo/desktop/src /environment/venv/bin/python \
  /repo/scripts/qualify_catalog_desktop.py \
  --desktop /environment/release-linux-v2/CommunityAI/CommunityAI \
  --output /environment/qualification/qwen-linux-catalog-startup-20260908 \
  --node-url http://127.0.0.1:18104
```

The output directory must be new. This exercise reads the existing public signed
sequence 1 and 2, preserves private fixture settings/cache, restarts, rejects the
old root and verifies runtime/credential cleanup. It requires no inference or
sharing load. Only a passing actual result can close the Linux startup slice.
The [first Linux attempt](qwen-catalog-linux-startup-failed-20260908.md) failed
the sequence-2 assertion and is preserved with its successful cleanup and
separate metadata-only diagnostics; those diagnostics do not close this slice.
A [fresh normal frozen desktop retry](qwen-catalog-linux-startup-20260908.md)
subsequently passed startup migration, unchanged-catalog restart, old-root
rejection, preferences/cache/native-credential preservation and cleanup.

The **periodic newer-sequence and active-generation** gap requires additional
inputs and a distinct result:

1. Stage disposable signed catalogs and exact approved manifests at an owned
   test HTTPS origin, with no changes to the production catalog or signing root.
   The production bootstrap validates public HTTPS URLs and port 443, so a plain
   localhost HTTP fixture cannot establish frozen transport acceptance. An
   operator-controlled test origin or another authorized, isolated transport
   environment must be arranged first; none is created by this checkpoint.
2. Start the verified frozen Linux node with private configuration, data, native
   credentials and test catalog trust. Keep sharing disabled and use local-only
   inference. For the no-load phase, publish a higher signed test sequence and
   observe the real configured refresh timer, immutable config change, node
   server restart and restored authenticated API. Recheck rejected signatures,
   rollback and equivocation without changing accepted state.
3. For the drain phase, use the existing verified local Qwen cache on CPU with
   one bounded request and one inference thread. Record an active request before
   advancing the test catalog. The already admitted request must finish under
   its original runtime; the server must not restart while it is active. Then
   observe shutdown/restart, new catalog policy, resumed authenticated API and
   preserved resource preferences/cache. This phase needs an explicit model
   memory/time allowance; inert source leases cannot replace it.
4. Retain exact binary/catalog/manifest hashes, old/new catalog sequences,
   request start/end and configuration/restart timestamps, output/token count,
   rejection outcomes, process identities and final credential/cache checks.
   Remove only the task's runtime, credential and test publication afterward.

Use the existing catalog refresh interval or declare a shorter **test-only**
interval in the private config. Do not edit the production interval, disable
signature validation, patch the frozen process, or claim this two-phase plan
has run merely because the source tests passed.

## Independent CI/source check

PR #26 head `23a1f99e81df5f98ee3260b7df2529332547ccca` had passing style, tests,
CodeQL and both complete production packaging jobs. The
[packaging run](https://github.com/flujo-app/CommunityAI/actions/runs/34175751433)
completed successfully; its audit bundles identify merge commit
`1b528212a85a58081e6e80bd855573673b3710ef`, tree
`60aa51a940433aebe97a7b57318e22a496f9f4db`. GitHub's comparison reports only
`README.md` changed between the reviewed head and that merge commit.

The application/catalog paths `src`, `desktop/src`, `public-alpha` and
`manifests` are unchanged between qualified Linux runtime source `bf67f0d` and
that reviewed head. CI archive provenance remains distinct from the previously
qualified installer bytes: only the small audit bundles were downloaded here,
not the multi-gigabyte archives. No installed acceptance is transferred to those
CI rebuilds by filename.
