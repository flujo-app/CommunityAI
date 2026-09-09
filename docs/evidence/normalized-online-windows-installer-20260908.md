# Production Windows online setup: full HTTPS handoff

**Passed for one complete public HTTPS download/install/CPU diagnostic/uninstall.**
The [companion record](normalized-online-windows-installer-20260908.json) binds
the exact launchers, downloaded-file hash, retained child-process handle, logs,
installed payload and cleanup. Execution began at `2026-09-09T01:29:46.701131+00:00`.
The online source commit is `b6c8aad9cea208630785d890cfb966093f809e7e`; an independent
Git blob comparison matched the builder, Inno script and helper source to the
production companion hashes. The offline runtime source is separately
`84205f93fc73d3babd39e238944b97fab0d11b3e`.

The online setup is **2,107,751 bytes**, SHA-256
`8ad0b7da83fdc7c32223902ed13d51f5f2946c89e00947e50aae742c1fc37861`. It downloaded the actual
**2,462,345,104-byte** offline installer from its
[embedded public R2 URL](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260908.1/communityai-0.1.0-alpha.20260908.1-windows-setup.exe). The entire
downloaded temporary file was independently read with a bounded buffer after
the owned child launched; its SHA-256 was
`116882e5d94e643e507efedebc4ec4b091275c5703f89bc646957f0f648d64bb`, matching the build-time pin.

| Check | Result |
| --- | --- |
| Native online download and child install | Passed in 1365.187 seconds. Outer exit 0 and independently retained offline-child handle exit 0. |
| Separate logs | Online log records the pinned URL and all downloaded bytes; offline log identifies the downloaded setup and the requested isolated install/log paths. Both report successful installation. |
| Installed payload | All 4,936 attested file lengths matched, totaling 4,263,859,354 bytes. GUI/node executable, CUDA 12.4 bitsandbytes library and normalization-report hashes matched provenance. |
| Installed CPU diagnostic | Passed in 2.813 seconds; frozen Torch 2.6.0+cu124 completed the exact CPU operation without a CUDA test, model load or network join. |
| Silent uninstall | Passed in 3.688 seconds. The application directory and owned installer processes were absent; persisted pre/post user-state metadata, menu and native registration snapshots matched exactly. |
| Temporary-file/handle cleanup | The downloaded full setup and its online temporary directory were removed after the waited child exited. The retained child-process query handle closed successfully. |

The run used an ordinary Windows user, hidden setup processes, two logical CPUs,
below-normal priority and separate online/offline installer logs. The pre-install
baseline was saved privately **before** launch; the post-uninstall baseline
matched it. Normal Start Menu
shortcuts were allowed and removed by the ordinary uninstaller; `/NOICONS` is not
a supported suppression option for this offline setup configuration.

The qualification also retained this best-effort presentation diagnostic:
`Skipped progress frame: IOException HRESULT 80070497`.
Progress display does not authorize installation; the complete file size, SHA-256,
successful helper exit and independent Inno recheck still gate the handoff.

This is the same offline payload that passed the
[installed required-CUDA/NF4 acceptance](normalized-windows-installer-20260908.md).
Earlier transport attempts remain separate: [first transfer](normalized-online-windows-download-failure-20260908.md), [isolated retry](normalized-online-windows-download-retry-failure-20260908.md). The earlier [progress-publication failure](normalized-online-windows-resumable-progress-failure-20260909.md) also remains separate. Repeating GPU checks was unnecessary. No product desktop window, model load,
network join, contribution worker or qualification credential was created.
Previous Gate 14/15 acceptance remains separate. The inherited source commit
identifies only the packaged offline runtime; online helper/Inno/builder working-tree
inputs are bound by companion hashes in the separate `online_source_identity`. This single transfer establishes
the observed production handoff, not broad network performance or availability.
