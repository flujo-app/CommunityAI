# Gate 16 Linux idle-stream transport follow-up — 2026-09-08

The focused RPC driver suite passes on Linux and Windows after a narrow idle-stream closure fix: **26 passed on each platform**. This is local source validation. Gate 16 remains open and no release artifact was rebuilt. Exact source and retained private-log hashes are in the [companion JSON](gate16-20260908-linux-idle-transport-fix.json).

At commit `64e4cf070dd8e5d19b9591e910e654094fa0f60b`, [Linux CI run 34212907798, job 102017840609](https://github.com/flujo-app/CommunityAI/actions/runs/34212907798/job/102017840609) reported one failure, 80 passes and four skips in the control-contract step. The failed case used an actual loopback TLS connection and the real admission handler. After the worker had released its idle lease, upstream Hivemind raised `ConnectionResetError` while the client completed its request producer and wrote end-of-stream to the expired connection. The same failure reproduced locally before the fix: one failed case in 11.19 seconds.

The driver now requires the client-observed idle duration to fall between the configured step timeout and that timeout plus five seconds, and verifies the exact accepted/rejected admission-counter changes before completing the producer. A reply already completed with a reset or another unexpected result fails. Only a `ConnectionResetError` raised while awaiting the subsequent producer-triggered closure is accepted, with a fixed `transport_closure` result category. Admission and malformed-request errors retain their existing strict handling; allocator guards, cache-capacity checks and cleanup remain in place.

| Focused validation | Result |
| --- | --- |
| Linux, upstream Hivemind 1.1.12, Python 3.12.14, pytest 9.1.1 | 26 passed in 12.65 seconds |
| Windows, patched Hivemind 1.1.12, Python 3.12.9, pytest 6.2.5 | 26 passed in 9.50 seconds |
| Black 22.3, isort 5.10, diff whitespace check | Passed for both modified source files |
| Independent read-only review | Passed after adding the completed-reset negative regression |

The nine new cases cover normal and reset closure after the timeout, early release, counter contamination, an already-completed reset before producer release, an unrelated exception, and resets during admission/malformed probes. Their proof-ordering fixtures use a deterministic clock and health snapshots. The existing real-handler and actual loopback TLS cases remain, including the backend allocator that raises if invoked and the missing-peer-cap regression.

Run the focused suite with:

```text
python -m pytest tests/test_gate16_live_rpc.py -q -p no:cacheprovider --disable-warnings
```

The Linux runs used a disposable container limited to two CPUs, 2 GiB memory, 128 processes and a 128 MiB temporary filesystem, with networking disabled except container loopback. The repository, existing environment volume and root filesystem were read-only; no GPU device or model-cache volume was attached. All owned test containers were removed. CI used Python 3.12.3 and pytest 6.2.5, so a fresh CI pass remains separate evidence from this local reproduction.

The [original 24-pass preparation record](gate16-20260908-driver-preparation.json) and its source hashes remain unchanged. The failed CI and before-fix logs, the initial 25-case runs, and the final 26-case runs are separately hashed in the companion record. Raw logs remain private.

The timing proof is client-observed and health is sampled: it establishes timeout-consistent release, not the remote transport's exact close cause. Health-to-peer pairing still requires the operator because health exposes no peer identity. No model, GUI, public worker, external route, credential or cloud resource was changed. The actual live inference, disclosure, catalog withdrawal/restore and other observations in the [Gate 16 runbook](../GATE16_CANARY.md) remain outstanding.
