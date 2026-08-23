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
- native credential-store access with automatic import of an existing headless-node key; and
- friendly automatic reconnect when the credential or node is unavailable.

Full first-run node configuration, node lifecycle supervision, contribution budgets,
login startup, accessibility validation, signed installers, and update/rollback behavior
remain later milestone-5 slices. The private-file bridge remains available only to the
headless `drift node`; this product package never accepts a control secret or secret-file
path on its command line.

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

On an existing headless-node installation, the desktop imports `control-api.key` into
the native credential store automatically. There is no setup button or secret prompt in
the normal app. Developers can still replace the stored key interactively without
putting it in the process list:

```shell
communityai-desktop --store-control-key
```

Then start the already-running node and launch `communityai-desktop`. The default node
URL is `http://127.0.0.1:8080`.

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
