# Packaged Windows catalog startup migration

**Passed**, September 7 local time (September 8 UTC), on ordinary non-elevated
Windows 10 with the qualified frozen desktop/node from `76b6d84`. The application,
node, model manifest and catalog source are unchanged through reviewed head
`e811036`. [Machine-readable evidence](qwen-catalog-desktop-20260907.json).

The replay installed the real online signed sequence 1 into a new private state
directory, then started the normal frozen Qt desktop with its bundled bootstrap.
It observed the visible native CommunityAI window and authenticated node status.
Without a manual catalog command, startup activated the exact published signed
sequence 2 and its explicitly authorized replacement trust root.

The desktop kept local-only mode, 37% VRAM, 43% processing, an 8 GiB storage limit,
worker preferences and a cache marker. Both Qwen entries became available;
legacy model entries remained explicit choices, while automatic selection used
the signed Qwen catalog priorities. Sharing remained paused. The first observed
window plus ready API took 63.406 seconds; repeat start took 46.781 seconds.
These are bounded startup observations, not isolated catalog-network timings.

After shutdown, a second normal launch preserved the configuration bytes and
native control credential. Supplying the former application trust root to the
packaged bootstrap CLI was rejected without changing the migrated configuration.
Both recorded GUI/node process trees exited, and the private native credential
was deleted. No model inference or sharing worker was started.

Replay:

```powershell
python scripts/qualify_catalog_desktop.py `
  --desktop <qualified-bundle>/CommunityAI.exe `
  --output <new-private-evidence-directory> --visible-ui
```

The script requires the desktop/client Python dependencies for orchestration and
uses the actual packaged executables for product behavior. It refuses elevation,
an existing GUI, an existing output directory or a reused native credential.
The maintained helper now defaults to offscreen Qt so an ordinary test invocation
does not open a desktop window. Explicit `--visible-ui` selects the native-window
acceptance shown above. This helper-only safety change came after the recorded
live replay; the evidence keeps that run's original script hash and visible-window
observations. An offscreen replay cannot claim visible-window acceptance.

The maintained helper also now records ownership immediately and uses a bounded
PID/creation-time cleanup fallback if `--prepare-update` fails. The replay remains
failed when fallback is needed, and its private credential is retained if owned
process absence cannot be established. Three focused tests cover descendants,
PID reuse, unreadable ownership and the finite deadline. This later helper change
was validated without launching Qt; it does not change the original replay hash
or claim that the recorded live run exercised emergency cleanup.

This closes the observed Windows **startup migration** slice. A newer catalog
arriving during an active generation, idle activation/drain and an ordinary-user
Linux update replay remain separate observations. Installer replacement is
covered by the existing Gate 15 evidence. The complete Q3.8 gate is not closed by
this replay.
