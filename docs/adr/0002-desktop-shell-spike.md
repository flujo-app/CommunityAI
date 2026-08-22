# ADR 0002: Desktop shell implementation spike

- Status: Proposed; spike in progress
- Date: 2026-08-22
- Roadmap: Milestone 5

## Context

Milestone 4 established `drift node` as the persistent, authenticated service boundary.
The desktop application must remain a client and lifecycle supervisor of that boundary;
it must not import model runtimes into the GUI process or manipulate contribution-worker
processes directly.

The roadmap requires an evidence-based choice between a Python-native Qt/PySide shell
and a webview shell with a Python sidecar. Choosing from source-code ergonomics alone
would leave the most consequential questions unanswered: packaging, signing, background
lifecycle, accessibility, native credential storage, update and rollback behavior, and
crash isolation on Windows, Linux, and macOS.

The current node also authorizes OpenAI inference and privileged `/control/v1/*`
operations with the same bearer-key set. That was an intentional milestone-4 bridge,
but it is not an acceptable final desktop boundary: an API key copied into an AI client
must not authorize worker controls or key administration.

## Spike decision

Build two deliberately small shells over one standalone Python node client:

1. a PySide 6 shell; and
2. a pywebview shell whose UI calls a Python bridge.

Both prototypes use only the versioned localhost control API. Neither imports `drift`,
Torch, Transformers, Hivemind, or model code. Each prototype must exercise the same
workflow and be packaged independently so its measurements include only its own shell
runtime.

The checked-in spike is not the product UI. It may be removed after the decision, while
the protocol client and acceptance contract can be promoted into the selected desktop
application.

## Fixed experiment

Each shell must demonstrate the following without changing the node service:

- load a privileged control credential from a native credential store, with an explicit
  private-file fallback for headless development;
- reject non-loopback node URLs before transmitting the credential;
- display the exact OpenAI base URL, node state, model state and route coverage, and
  contribution-worker state;
- copy the OpenAI endpoint, create a labeled client API key, and display its secret once;
- start, pause, and restart a configured contribution worker through `/control/v1`;
- keep network work outside the GUI event loop;
- report connection and protocol errors without exposing the control credential;
- reconnect after the GUI or node is restarted; and
- produce independently measurable application bundles on Windows, Linux, and macOS.

A shared headless acceptance harness validates authentication, status parsing, client-key
lifecycle, and worker transitions. Packaged binaries expose a runtime check so CI verifies
that the selected GUI framework was actually collected.

## Measurements and decision gates

For each operating system and shell, record:

| Gate | Measurement |
| --- | --- |
| Packaging | Compressed and installed bundle bytes, file count, clean-machine prerequisites |
| Startup | Cold start to usable window, idle RSS, background/tray RSS |
| Isolation | GUI crash leaves the node and worker alive; worker crash leaves the GUI responsive |
| Lifecycle | Login startup, single-instance behavior, clean shutdown, node reconnect |
| Updates | Signed install, signed upgrade, rollback, uninstall, and retained user data |
| Accessibility | Keyboard-only workflow, labels/roles, focus order, screen-reader smoke |
| Native integration | Credential store, tray/menu bar, notifications, protocol links |
| CI | Reproducible package build and runtime smoke on Windows, Linux, and macOS |

The selected shell must pass every functional gate. Prefer the smaller and operationally
simpler result if both pass. A modest source-code convenience does not outweigh a failed
security, accessibility, signing, update, or crash-isolation gate.

The repository CI produces unsigned spike bundles and size manifests. Signing and update
tests require platform signing identities and remain a mandatory pre-release gate, not a
reason to embed test credentials in the repository.

### Preliminary Windows evidence

On the initial Windows 10 development run, both source shells and both packaged binaries
completed the authenticated UI self-test against an isolated fake node. PySide 6.11.2
produced an approximately 125.1 MB onedir bundle; pywebview 6.2.1 produced an approximately
40.0 MB bundle using the installed WebView2 runtime. These are directional results only:
the local measurement environment contained both shell dependencies. The CI matrix builds
each shell in a clean job and is the decision-grade source for bundle comparisons.

## Security boundary required before productization

The prototypes can connect to the milestone-4 API, but the production desktop must first
separate two authorities:

- a privileged desktop control credential, generated during local installation and kept
  in the native credential store; and
- revocable OpenAI inference keys that users copy into AI clients.

Only the control credential may call `/control/v1/*`. OpenAI keys may call `/v1/*` and
nothing else. The desktop must never accept either secret in a command-line argument,
write it to logs, interpolate it into HTML, or send it to a non-loopback URL.

## Non-goals

This spike does not choose the final visual design, implement autonomous cross-model
allocation, add public catalogs or credits, bundle model runtimes into the GUI, or relax
the release gates in `docs/REVIVAL.md`.

## Outcome template

When the three-platform measurements are available, replace the status above with
`Accepted` and record:

- selected shell and rejected alternative;
- artifact and runtime measurements by platform;
- accessibility and crash-isolation results;
- signing, upgrade, rollback, and uninstall results;
- native credential-store behavior; and
- any architectural consequence for the node/control API.
