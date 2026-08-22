# CommunityAI desktop shell spike

This standalone package compares the two desktop-shell candidates from
[`ADR 0002`](../../docs/adr/0002-desktop-shell-spike.md). It intentionally does not
depend on the `drift` package: both shells communicate with an already-running local
node through `/control/v1`.

## Install

Create a disposable virtual environment inside this directory, then install one shell:

```shell
python -m pip install -e ".[pyside,dev]"
```

or:

```shell
python -m pip install -e ".[webview,dev]"
```

The native credential-store adapter uses the operating-system backend exposed by
`keyring`. Store the node control credential without putting it in the process list:

```shell
communityai-desktop-spike --store-control-key
```

For a headless development node, its existing private key file can be selected instead:

```shell
communityai-desktop-spike --shell pyside --control-key-file /path/to/local-api.key
communityai-desktop-spike --shell webview --control-key-file /path/to/local-api.key
```

The default endpoint is `http://127.0.0.1:8080`. A URL ending in `/v1` is accepted and
normalized. Non-loopback URLs are rejected before the control credential is read.

## Verification

The contract test has no GUI dependency:

```shell
python -m unittest discover -s tests -v
communityai-desktop-spike --self-test
```

Verify that an installed shell runtime can be imported:

```shell
communityai-desktop-spike --shell pyside --check-runtime
communityai-desktop-spike --shell webview --check-runtime
```

Build and smoke-test independent PyInstaller bundles:

```shell
python build_spike.py --shell pyside
python build_spike.py --shell webview
```

The build writes a JSON size manifest beside each bundle. GitHub Actions repeats this
on Windows, Linux, and macOS. These are unsigned experimental bundles, not releases.
