# Packaged Linux catalog startup migration

**Passed on retry**, September 8 UTC, with the qualified frozen desktop/node
from `bf67f0d`, as UID 1000 in Ubuntu 22.04 with a private DBus session and native
gnome-keyring credentials. Qt was offscreen; no visible Linux window is claimed.
The [machine-readable result](qwen-catalog-linux-startup-20260908.json) binds the
actual executable, bootstrap and replay helper hashes. The
[first failed attempt](qwen-catalog-linux-startup-failed-20260908.md) remains
preserved separately; its bootstrap child failure cause is unconfirmed.

The retry started with a new private fixture installed from the actual online
signed sequence 1. A normal frozen desktop launch, without a manual refresh of
that fixture, activated the exact published sequence-2 catalog and its explicitly
authorized replacement trust root. Both Qwen entries appeared in authenticated
node status. First startup took 22.835 seconds; the second took 24.480 seconds.
These timings include desktop, bootstrap and node readiness under concurrent
installer unpacking, rather than isolating catalog-network time.

The replay preserved local-only mode, 37% VRAM, 43% processing, an 8 GiB storage
limit, worker preferences and a cache sentinel. The worker stayed paused and
sharing remained disabled. Restart preserved config bytes and the native
control credential. The old application root was then rejected by the frozen
bootstrap CLI without changing the migrated config. All recorded runtime trees
stopped, and the private credential was deleted. Defunct Linux processes are
treated as non-executing; PID and creation-time checks still protect unrelated
processes from cleanup.

Replay, inside the existing ordinary-user DBus/unlocked-keyring session:

```sh
PYTHONPATH=/repo/desktop/src /environment/venv/bin/python \
  /repo/scripts/qualify_catalog_desktop.py \
  --desktop /environment/release-linux-v2/CommunityAI/CommunityAI \
  --output /environment/qualification/qwen-linux-catalog-startup-20260908-retry \
  --node-url http://127.0.0.1:18104
```

The output directory and private credential must be new. The helper refuses
root and an executing existing desktop. The frozen bundle was launched directly;
its installer lifecycle is separate evidence. No model inference, active worker,
periodic newer-sequence activation or active-generation drain ran here. This
closes the observed Linux startup-migration slice; the complete Q3.8 gate still
needs the separate [periodic live replay](qwen-catalog-periodic-source-20260908.md).
