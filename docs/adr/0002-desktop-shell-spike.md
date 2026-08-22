# ADR 0002: Desktop shell implementation spike

- Status: Accepted; PySide 6 selected for product implementation
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

The milestone-4 node initially authorized OpenAI inference and privileged
`/control/v1/*` operations with the same bearer-key set. That was an intentional
bridge, but it is not an acceptable final desktop boundary: an API key copied into
an AI client must not authorize worker controls or key administration. The first
milestone-5 security slice below now closes that gap.

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

### Cross-platform packaging evidence

The clean [three-platform CI run](https://github.com/flujo-app/CommunityAI/actions/runs/32595385317)
built each shell independently and ran its packaged runtime check and authenticated UI
self-test. All six jobs passed. Sizes below are uncompressed onedir bundle bytes; all
artifacts are unsigned.

| Platform | PySide 6.11.2 | pywebview 6.2.1 | Result |
| --- | ---: | ---: | --- |
| Windows x64 | 128,862,316 bytes / 236 files | 29,234,555 bytes / 215 files | Both passed |
| Linux x64 | 324,323,392 bytes / 367 files | 948,268,266 bytes / 1,942 files | Both passed |
| macOS arm64 | 210,278,876 bytes / 268 files | 43,560,981 bytes / 106 files | Both passed |

The Linux jobs install `libegl`, `libgl`, DBus, and xkbcommon as explicit clean-runner
prerequisites. The build harness preserves packaged-process stdout and stderr on failure,
so missing runtime libraries do not collapse into an opaque exit code.

## Outcome

Select **PySide 6** for the production desktop shell and retire the pywebview alternative
after promoting the shared node client and acceptance contract.

Pywebview has a compelling size advantage on Windows and macOS, but its Linux Qt package
is approximately 2.9 times the PySide package and contains more than five times as many
files. It also introduces a JavaScript bridge and three renderer/backend families across
the supported platforms. PySide provides one Python-native widget and accessibility model,
one asynchronous task boundary, and the more consistent cross-platform packaging result.
That consistency outweighs its larger Windows and macOS bundles for this product.

This selection closes the shell-comparison gate, not the release gates. Productization
must still measure cold startup and RSS, prove GUI/node/worker crash isolation, implement
login startup and single-instance lifecycle behavior, use each operating system's native
credential store, complete keyboard and screen-reader testing, and pass signed installer,
upgrade, rollback, uninstall, and retained-data tests. No unsigned spike artifact is a
release candidate.

## Security boundary implemented before productization

The production desktop boundary now separates two authorities:

- a privileged desktop control credential, generated during local installation and kept
  in the native credential store; and
- revocable OpenAI inference keys that users copy into AI clients.

The node now enforces this split: only the control credential may call
`/control/v1/*`, while managed OpenAI keys may call `/v1/*` and nothing else. Startup
rejects missing, duplicate, or overlapping control credentials. The headless bridge
uses a private `control-api.key`; the selected desktop will replace that bridge with
the operating-system credential store. The desktop must never accept either secret
in a command-line argument, write it to logs, interpolate it into HTML, or send it to
a non-loopback URL.

## Non-goals

This spike does not choose the final visual design, implement autonomous cross-model
allocation, add public catalogs or credits, bundle model runtimes into the GUI, or relax
the release gates in `docs/REVIVAL.md`.
