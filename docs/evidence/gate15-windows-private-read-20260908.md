# Frozen Windows checkbox read on a private desktop — 2026-09-08

The unmodified qualified Windows desktop exposed **Start CommunityAI when I sign
in** as an enabled UI Automation checkbox, initially **Off**. Its normal managed
node authenticated successfully in 16.765 seconds with zero resident models,
paused contribution, and signed catalog sequence 2. See the
[passing record](gate15-windows-private-read-20260908.json) for exact executable,
bootstrap and replay hashes.

The GUI and a windowless MTA UI Automation actor ran on the same new private
Windows desktop. Both were created suspended and assigned to a kill-on-close job
before resuming. The helper never requested desktop-switching rights or injected
input. The user's input desktop remained `Default`. The normal desktop maintenance
command stopped the managed tree; the job contained zero active processes before
closure, both desktop/job handles closed, and the unique native credential was
removed. The real Run value was absent before and after this read-only test. A
[separate root-agent audit](gate15-windows-read-independent-audit-20260908.json)
confirmed all five attempts' recorded identities were gone and their unique
credentials absent.

Four earlier failed harness attempts remain recorded:

1. [Initial enumeration failure](gate15-windows-private-read-failed-20260908.json).
2. [Thread-only desktop attachment attempt](gate15-windows-private-read-second-failed-20260908.json).
3. [Same-desktop actor, first native binding error](gate15-windows-private-read-third-failed-20260908.json).
4. [Successful enumeration, incorrect Sharing role locator](gate15-windows-private-read-fourth-failed-20260908.json).

The native-boundary diagnostic separately reproduced .NET Framework's first
callback binding reporting error 127 on an empty private desktop. Reusing a
callback and warming that binding before measured calls produced false/error 0
for an empty desktop and successful enumeration of a controlled private window.
Its [result](gate15-windows-private-enumeration-20260908.txt) used no CommunityAI
process or registry mutation. Subsequent measured errors remain fatal except
bounded empty-desktop retries.

Qt exposes the checkable Sharing navigation button as `ControlType.CheckBox`.
The corrected locator matches its exact name within the exact owned window and
uses the supported `InvokePattern.Invoke`. This exposed the actual sign-in
checkbox. These failures diagnose the qualification harness; they do not establish
a product checkbox failure. No sign-in checkbox mutation occurred in these five
attempts, so this record alone does not establish enable/restart/disable behavior.

The approach follows Microsoft's guidance on a
[windowless MTA UIA client](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)
and [process/thread desktop association](https://learn.microsoft.com/en-us/windows/win32/winstation/thread-connection-to-a-desktop).
