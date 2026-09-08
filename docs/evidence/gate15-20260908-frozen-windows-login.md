# Frozen Windows sign-in checkbox cycle — 2026-09-08

**Passed:** the unmodified qualified Windows desktop enabled login startup,
preserved it across a normal shutdown and fresh GUI launch, then disabled it.
The [sanitized acceptance record](gate15-20260908-frozen-windows-login.json) contains
the exact desktop/node/bootstrap and qualification source hashes, both phase
results, owned PID/creation identities, and cleanup proof.
Its source-result SHA-256 identifies the retained local raw record. The public
copy omits only the unique credential service/account identifiers and absolute
local executable path; it retains the verified startup argument and registry type.

| Phase | Real Qt checkbox | Native registry verification | Readiness |
| --- | --- | --- | --- |
| Enable | Off → On | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\CommunityAI` exactly matches the qualified frozen executable plus `--started-at-login`, type `REG_SZ` | 17.359 seconds |
| Fresh launch, then disable | Initially On → Off | The CommunityAI Run value is absent | 17.375 seconds |

Both launches used the same fresh signed sequence-2 state and the same unique
Windows native credential. The credential and private node configuration remained
unchanged through each normal shutdown. Each authenticated normal managed node
reported running, zero resident models, local-only inference and paused
contribution; no model weight files were downloaded.

Each GUI and its UI Automation actor ran on a new unswitched private Windows
desktop. The actor found exactly one checkbox with the expected accessible name
and role. Before each `TogglePattern.Toggle`, Python verified the exact expected
Run state through a ready/go handshake. The user's input desktop remained
`Default`; no input injection or desktop switching was used. The qualified app
and its UI were not patched.

Normal `--prepare-update` shutdown stopped each owned tree. Each kill-on-close job
had **zero active processes before closing**, and its desktop/job handles closed.
All 18 recorded process identities were gone before final credential removal. The
original Run state was absence and was restored by the product's disable action;
the restoration guard verified it without an additional registry write. The
temporary native credential was then deleted and independently read as absent.
The [separate root-agent audit](gate15-windows-cycle-independent-audit-20260908.json)
confirmed all 18 exact process identities were gone, both jobs were empty before
closing, the credential was absent, the original Run absence was restored, and
the executable hashes matched the qualified Windows artifacts.

The default replay remains read-only. The cycle requires explicit opt-in:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'desktop/src'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
.gate13-runs/qwen-product-venv/Scripts/python.exe scripts/qualify_login_startup_windows.py `
  --desktop .gate13-runs/gate14-release-windows-v2-output/CommunityAI/CommunityAI.exe `
  --output .gate13-runs/gate15-windows-login-cycle-NEW-RUN `
  --node-url http://127.0.0.1:18118 --exercise-login-startup
```

Use a new output directory and an unused localhost port with all other
CommunityAI desktops stopped. The original Run value/type is saved privately for
recovery. Restoration refuses an observed unrelated registry change; missing job
cleanup proof retains the private credential and fails acceptance. The 46
focused ownership, restoration and cycle-finalizer tests passed before this run;
the pure safety tests also passed on Linux.

This verifies the shipped checkbox and native registration across an application
restart. It does not perform a Windows logout/login or claim that an actual OS
sign-in session was exercised. The earlier read-only diagnostic attempts remain
available in the [read-only evidence](gate15-windows-private-read-20260908.md).
