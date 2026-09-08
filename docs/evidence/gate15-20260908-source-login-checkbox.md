# Offscreen sign-in checkbox regression

**Source regression passed; frozen Windows acceptance remains open.** The
[evidence](gate15-20260908-source-login-checkbox.json) preserves that distinction.

The actual source Qt checkbox enabled a registration in a newly created native
Windows qualification key. A recreated window read it as enabled, and a second
literal checkbox action disabled it. Labels followed Off → Enabled for this
user → Off. The test key was removed, while the user's real CommunityAI login
entry stayed unchanged. The key was outside Windows' autostart location, so it
could not launch anything at sign-in. No credential, node, inference, fixture
telemetry or visible window was created.

Three offscreen desktop tests also cover saved-state reflection, failed-write
reversion and disabled controls when registration cannot be read. They run with
the normal desktop unittest suite.

The helper read the final Windows executable's embedded PyInstaller bytecode
without running or changing it. Its resource qualification hook accepts only
`observe`, `limits`, `start` and `pause`; its CLI has no sign-in-toggle test action.
Source testing cannot close that frozen boundary. The earlier interrupted
[Windows UI attempt](gate15-20260908-windows-data-login-choices.json) is retained.

Repeat the bounded source check with the desktop development environment:

```powershell
python scripts/qualify_login_startup_source.py `
  --desktop <qualified-bundle>/CommunityAI.exe `
  --output <new-private-evidence-directory>
```

The executable is used only for read-only hook inspection. Qt is forced
offscreen, and the helper refuses an already initialized native Qt platform.
