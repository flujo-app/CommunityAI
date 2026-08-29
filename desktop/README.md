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
- source-level startup, readiness, crash-backoff, reconnect, and owned-process shutdown for the standalone node;
- one per-user desktop owner, with manual second launches activating the existing window and login launches remaining silent; and
- opt-in native per-user login startup through Windows Run, a macOS LaunchAgent, or Linux XDG autostart.

The desktop starts `drift node` with an explicit native credential source and never puts
the secret in its command, environment, logs, or ordinary configuration. A migrated
private file is removed only after a desktop-owned native-key node authenticates. File
mode remains the default for explicitly headless `drift node` use. The Sharing page can
register the exact current executable to start minimized after sign-in. Registration is
shell-free, rejects control-character injection and unsafe link targets, and an
ownership lock ensures a second desktop never starts or stops another local node.

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
The first signed public-alpha bootstrap and its exact Qwen/Gemma manifests are
published under [`public-alpha/catalog-v1`](../public-alpha/catalog-v1). Production
desktop CI verifies and bundles those inputs; an input-free local engineering build
remains available and honestly renders the missing-catalog state on a truly clean
install. See [`CATALOG_BOOTSTRAP_V1.md`](../docs/CATALOG_BOOTSTRAP_V1.md).

Cross-platform packaged validation of native-store promotion and the new
single-instance/login-startup behavior, contribution budgets, accessibility validation,
signed installers, and update/rollback behavior remain later milestone-5 gates.

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

Once release catalog inputs are qualified, stage the complete deterministic publication
bundle into the product with:

```shell
python build_desktop.py \
  --publication-bundle ../public-alpha/catalog-v1 \
  --source-commit <full-git-object-id> \
  --build-workflow local
```

The builder revalidates the bundle index plus the exact signed catalog, bootstrap,
manifests, and publication preflight before PyInstaller starts. It then reloads the
actual packaged copy and requires its complete evidence to match the pre-copy evidence
before recording catalog/bootstrap identity, the bundle-index digest, member count, and
member digests in `desktop-metrics.json`. After every packaged smoke passes, it
also writes a sorted `SHA256SUMS` inventory for exact regular files under
`CommunityAI/`, source/build/catalog-bound `provenance.json`, and
`release-metadata.json` with an explicit unsigned public-alpha warning. Verify a
completed output in a fresh process with:

```shell
python build_desktop.py \
  --verify-release-output dist/desktop \
  --publication-bundle ../public-alpha/catalog-v1 \
  --source-commit <full-git-object-id> \
  --build-workflow local \
  --verify-build-environment
```

Supplying the expected inputs makes the fresh process revalidate the catalog bundle and
require the recorded source commit/tree, workflow, platform, Python, and PyInstaller
identity to match. Omitting them performs only structural metadata and payload
verification.

These checksums identify the exact emitted bytes; they are not a publisher signature or
a claim that two independent PyInstaller environments produce identical bytes. The
retained `complete_release_qualification=false` value makes clear that this repository
audit bundle is not model, worker, public-infrastructure, or packaged-inference
qualification.

On Windows the GUI build uses the GUI subsystem, so double-clicking the application does
not open a terminal. The build runs the packaged GUI runtime check, headless control-API
contract, connected UI smoke, automatic-reconnect smoke, and the frozen node/worker
runtime checks. It also writes GUI and sidecar size/runtime evidence to `dist/desktop`.
These bundles are checksum-verifiable unsigned public-alpha engineering evidence, not
publisher-signed installers or completed release qualification.
