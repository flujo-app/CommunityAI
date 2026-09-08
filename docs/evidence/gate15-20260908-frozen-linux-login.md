# Frozen Linux sign-in checkbox acceptance

**Passed for the tested Linux scope.** The unmodified frozen Qt application
enabled, persisted and disabled sign-in startup through its AT-SPI accessibility
interface. [Machine-readable evidence](gate15-20260908-frozen-linux-login.json).

The test used the verified Linux bundle from `bf67f0d` as an ordinary Ubuntu
22.04 container user, on a private Xvfb display and D-Bus/Secret Service session.
Private XDG directories kept the test registration separate from other desktop
state. There was no host Windows window or Computer Use interaction.

The actual application bootstrapped the signed Qwen catalog and connected to its
actual authenticated node. Sharing was paused and local-only mode was selected.
Each observed phase reported zero resident models and zero cache files/bytes;
the final cache check was also empty. No fixture telemetry was supplied.

The accessibility actor selected the exact owned GUI process, pressed its Sharing
navigation control, and invoked the reported Toggle action of **Start CommunityAI
when I sign in**. It checked both the checkbox state and the exact autostart-file
contents naming the qualified frozen executable with `--started-at-login`.
A new GUI process read the saved enabled state; toggling it again removed the
file. A third real launch with `--started-at-login` reached authenticated node
readiness and exposed the owned accessibility application. Minimized/tray
appearance and actual OS sign-out/sign-in were not qualified without a window
manager.

All three product shutdowns completed normally with exit code 0. Fourteen recorded
GUI/node identities stopped, and the private credential and autostart file were
removed. An independent process audit passed; a fresh Secret Service session
independently confirmed that the test credential was absent.

Two harness limitations remain explicit. The
[first actor attempt](gate15-20260908-linux-login-actor-failure.json) refused a
valid control exposing both Press and Toggle. The corrected actor selects Press
for navigation and Toggle for the checkbox, with a regression test. After the
passing replay, auxiliary cleanup stopped Xvfb before its wrapper finished, so
the outer wrapper returned 1 even though the product driver returned 0. The
independent cleanup audits passed afterward. Neither issue is reported as a
product failure or silently discarded.

The maintained helper is `scripts/qualify_login_startup_linux.py`; its 12 tests
cover exact PID selection, unique name/role lookup, bounded malformed trees,
private-display validation and explicit Qt action selection. Run it only inside
an owned Xvfb session with `QT_QPA_PLATFORM=xcb`, a private `XAUTHORITY`,
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_RUNTIME_DIR`, unlocked Secret Service,
and `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`. System `python3-pyatspi` supplies the
accessibility actor; the desktop Python environment supplies the host's client
and credential libraries. The helper refuses root and unbound/inherited displays,
limits acceptance to 540 seconds plus finite cleanup, and treats forced runtime
cleanup as a failed acceptance.

The package's installation/removal lifecycle remains separate. This Linux result
also does not close the [frozen Windows checkbox gap](gate15-20260908-source-login-checkbox.md).
