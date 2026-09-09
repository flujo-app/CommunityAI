# Source checkbox portability follow-up

**Passed:** the corrected source helper passes all three checkbox tests on
Windows and the complete Linux desktop suite: 108 tests in 27.099 seconds, with
the two existing root-only installer tests skipped. The
[structured evidence](gate15-20260908-source-login-portability.json) preserves
the failed baseline and final checks.

The production packaging job at commit `64e4cf0` exposed two Linux failures.
Reproduction showed that the helper left the checkbox on the hidden Sharing
page, with an unlaid-out 640 × 480 widget. Its default center was outside the
Linux style's 271-pixel clickable region. The mouse event therefore triggered
neither a state change nor the expected failed-write warning.

The helper now shows Sharing in its offscreen window, scrolls the checkbox into
view and sends a literal `QTest.mouseClick` to the style-defined indicator
center. It checks visibility and that the point lies inside both the widget and
the clickable region. The tests retain their saved-state, write-history and
warning assertions.

A full-suite rerun also exposed earlier sessions' uncancelled static auto-close
timers interrupting subsequent event loops. The source helper now owns its
watchdog and exercise timers, stopping and disconnecting them on every return.
The final full suite passed after correcting the isolated runner's import path
and Git safe-directory configuration. Black, isort and whitespace checks passed.

Linux runs used disposable, network-disabled containers limited to two CPUs and
2 GiB, with read-only repository/dependency mounts and private temporary home
and XDG directories. Product source, real login entries and native credentials
were unchanged. The earlier
[Windows source evidence](gate15-20260908-source-login-checkbox.json), including
its original helper hash, remains intact. The separately recorded
[frozen Windows acceptance](gate15-20260908-frozen-windows-login.md) remains the
packaged checkbox result.
