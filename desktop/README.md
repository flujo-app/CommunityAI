# CommunityAI desktop

This is the production PySide client for the standalone CommunityAI local node. It
uses only the authenticated, versioned `/control/v1` HTTP API and deliberately does
not import `drift`, Torch, Transformers, Hivemind, or model code.

The desktop currently provides the promoted milestone-5 vertical slice:

- strict loopback-only control traffic with redirects and HTTP proxies disabled;
- a modern Home, Models, Sharing, and API-access experience;
- available models, total peers, optional peer-region counts, and current contribution status;
- one-click model selection plus start and pause controls for contribution workers;
- a persistent GPU-memory target slider ready for node-side budget enforcement;
- create, relabel, and revoke controls for OpenAI client keys;
- one-time display and clipboard copy for newly created client-key secrets; and
- native credential ownership with verified generation or automatic import of an existing headless-node key;
- source-level startup, readiness, crash-backoff, reconnect, and owned-process shutdown for the standalone node; and
- friendly automatic reconnect when the credential or node is unavailable.

The desktop starts `drift node` with an explicit native credential source and never puts
the secret in its command, environment, logs, or ordinary configuration. A migrated
private file is removed only after a desktop-owned native-key node authenticates. File
mode remains the default for explicitly headless `drift node` use.

The PyInstaller product bundle now contains the GUI plus a separately built
`node/CommunityAI-Node` runtime. The node bundle retains Torch, Transformers,
Hivemind, the platform keyring backend, and `p2pd`, while the GUI executable continues
to exclude those packages. The build smokes both the node and contribution-worker
entry points. A packaged Windows lifecycle smoke has also launched that sidecar with a
native Credential Manager key, joined the published DNS seed, authenticated readiness,
and shut down the owned process without writing a control-key file.

The sidecar now implements the first-install catalog consumer, and the desktop invokes
it automatically when `~/.drift/node/node-config.json` is absent. It authenticates a
bounded HTTPS catalog against a bundled root, enforces expiry and persistent rollback
state, installs only exact digest-matched manifests, generates the seed-backed node
configuration, and retains an unexpired last-known-good catalog for offline recovery.
The release bootstrap file and first qualified public manifests are not published or
bundled yet, so current unsigned builds still render the missing-catalog state on a
truly clean install. See [`CATALOG_BOOTSTRAP_V1.md`](../docs/CATALOG_BOOTSTRAP_V1.md).

Cross-platform packaged native-store promotion, contribution budgets, login startup,
accessibility validation, signed installers, and update/rollback behavior remain later
milestone-5 slices.

## Development

Create a disposable environment and install the package:

```shell
python -m pip install -e "..[api]"
python -m pip install -e ".[dev]"
```

On Windows, install the repository's patched Hivemind wheel first as described in the
root README. The desktop source tests need only the desktop package; producing the node
sidecar requires the full root runtime.

Run the headless protocol and source-boundary tests:

```shell
python -m unittest discover -s tests -v
communityai-desktop --self-test
```

Exercise the real installed node process, native OS credential backend, published DNS
seed, authenticated readiness, and owned shutdown:

```shell
python ../scripts/smoke_desktop_managed_node.py
```

On an existing headless-node installation, the desktop imports `control-api.key` into
the native credential store automatically. On a fresh installation it creates the
control credential in that store directly. There is no setup button or secret prompt in
the normal app. Developers can still replace the stored key interactively without
putting it in the process list:

```shell
communityai-desktop --store-control-key
```

The desktop starts and owns the source node automatically when the default
`~/.drift/node/node-config.json` exists. The default node URL is
`http://127.0.0.1:8080`. Pass the hidden development flag `--no-manage-node` to attach
to an already-running headless node without taking lifecycle ownership.

Build and smoke-test the unsigned PyInstaller bundle:

```shell
python generate_assets.py  # only needed after changing the product icon
python build_desktop.py
```

Once release catalog inputs are qualified, stage the validated public bootstrap file
into the product bundle with:

```shell
python build_desktop.py --bootstrap-config ../path/to/catalog-bootstrap.json
```

On Windows the GUI build uses the GUI subsystem, so double-clicking the application does
not open a terminal. The build runs the packaged GUI runtime check, headless control-API
contract, connected UI smoke, automatic-reconnect smoke, and the frozen node/worker
runtime checks. It also writes GUI and sidecar size/runtime evidence to `dist/desktop`.
These bundles are CI evidence, not signed installers or release candidates.
