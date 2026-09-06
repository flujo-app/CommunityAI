# Qwen mixed-cloud inference

For the combined source and packaged Windows C3 recovery/cache test, use
[Run Qwen Product Test](QWEN_PRODUCT_TEST.md). The runner below is the original
mixed full-inference proof.

Run `Run Qwen Mixed Inference.cmd` or:

```powershell
python scripts/run_qwen_mixed_inference.py
```

The runner refuses to provision a mixed swarm until a retained CPU run proves
complete inference, same-session recovery on a fresh worker identity, and
verified cleanup. It records the SHA-256 of the qualifying CPU result.

| Role | Provider and VM | Blocks |
| --- | --- | --- |
| Client/coordinator | GCP e2-standard-4 | Embeddings, final norm and output head |
| Worker 0 | GCP g2-standard-8, L4 | 0–15 |
| Worker 1 | Azure Standard_NC4as_T4_v3, T4 | 16–31 |
| Worker 2 | GCP e2-highmem-4 | 32–47 |
| Worker 3 | GCP e2-highmem-4 | 48–63 |

The mixed run uses the same pinned FP8 checkpoint and BF16 eager execution
profile as the CPU test. Actual GPU identity, memory, compute capability and
BF16 matrix/convolution execution are checked before loading model shards.
A successful kernel probe does not imply native BF16 acceleration on the T4.
The full inference must use the inspected L4, T4 and two CPU peer identities.

For product recovery qualification, `scripts/run_qwen_product_mixed.py` also
accepts `--cpu-worker-machine-type c3-highmem-4`. This changes only the two CPU
remainder VMs: each still has four vCPUs and 32 GB. Preflight accounts for eight
C3 vCPUs, four E2 coordinator vCPUs and eight L4-host vCPUs (20 GCP vCPUs total).
C3 requires balanced disks and gVNIC, which the runner selects explicitly;
the supported disk/network restrictions are documented by
[Google](https://docs.cloud.google.com/compute/docs/general-purpose-machines#c3_series).
Existing quota must suffice; catalog thresholds are unchanged. The default and
original CPU proof continue to use E2. Preserve the selected provider profile
with each result rather than treating these CPU families as equivalent evidence.

The source snapshot and package inventory are retained. GPU workers install
PyTorch 2.6.0 with CUDA 12.4; CPU hosts install its CPU build. GCP uses the pinned
Ubuntu image with a balanced boot disk. Both GPU hosts install Ubuntu's
version-pinned NVIDIA 580 server driver and verify it with `nvidia-smi`.
The model artifacts are acquired by the production manifest verifier.
G2 disk and image choices follow Google's
[G2 restrictions](https://docs.cloud.google.com/compute/docs/accelerator-optimized-machines#g2_limitations).

Swarm peers advertise public addresses to cross the provider boundary. TCP
31330 ingress is restricted to the five run-specific public IPs. GCP SSH uses
IAP; Azure SSH is limited to the launching computer's observed public IP and
uses an ephemeral key retained only in the ignored run directory. Workers do
not receive cloud credentials or Hugging Face credentials.

Preflight checks existing quota in both providers; it never requests more.
The runner registers `Microsoft.DevTestLab` if needed for VM auto-shutdown;
the provider registration remains enabled after resource cleanup.
Resources receive a unique run tag. GCP VMs have a maximum lifetime and Azure
has an automatic shutdown backstop. Normal cleanup deletes the exact owned
GCP resources and the dedicated Azure resource group, including its disks and
network resources, and verifies absence. The next invocation resumes incomplete
cleanup before creating anything. The shutdown backstop alone does not remove
Azure disks or public IPs.

Evidence is retained under `.gate13-runs/qwen-mixed/<run-id>/`, including
`workers.json`, `client-result.json`, `result.json`, and `cleanup.json`.
This short functional experiment does not constitute GPU performance
qualification. An attempted run that fails a kernel probe, loading or routing
remains a failed result with its diagnostic evidence.

Explicit cleanup:

```powershell
python scripts/run_qwen_mixed_inference.py --cleanup-run C:\path\to\run-directory
```
