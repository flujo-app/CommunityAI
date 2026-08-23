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

The current PyInstaller artifact still contains only the GUI client: the real model/DHT
node sidecar and initial signed catalog have not been added to that bundle. Until those
arrive, source runs can supervise an installed node when
`~/.drift/node/node-config.json` exists, while a fresh packaged install renders a
model-catalog or missing-sidecar state in the window. Contribution budgets, login
startup, accessibility validation, signed installers, and update/rollback behavior
remain later milestone-5 slices.

## Development

Create a disposable environment and install the package:

```shell
python -m pip install -e ".[dev]"
```

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

On Windows the build uses the GUI subsystem, so double-clicking the executable does not
open a terminal. The build runs the packaged runtime check, headless control-API contract,
connected UI smoke, and automatic-reconnect smoke. It also writes a size and
runtime manifest to `dist/desktop`. These bundles are CI evidence, not signed installers
or release candidates.
