# CommunityAI

[![Version](https://img.shields.io/badge/version-2.3.0.dev2-6d4aff)](https://github.com/flujo-app/CommunityAI)
[![Tests](https://github.com/flujo-app/CommunityAI/actions/workflows/run-tests.yaml/badge.svg?branch=main)](https://github.com/flujo-app/CommunityAI/actions/workflows/run-tests.yaml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e.svg)](LICENSE)

**AI powered by people.**


![CommunityAI sharing screen](desktop/dist/communityai-sharing-final.png)

## How it works

Community-AI is a shared Large-Language-Model, by the people, for the people.

1. Start the app.
2. Connect an OpenAI-compatible client to the local endpoint.
3. Use public community inference and optionally share compute within your limits.

Community-AI takes care of everything else.

The application ships one model-agnostic runtime. Its signed catalog approves exact model
manifests; when a model is selected, CommunityAI downloads only the upstream Hugging Face
checkpoint files needed by the local client components or contributed block range, verifies
their declared size and SHA-256, and keeps them in a persistent shared cache. It does not need
one installer or container image per model. Download minimization is currently limited to
whole upstream checkpoint shards. See
[`ADR 0003`](docs/adr/0003-direct-manifested-artifact-delivery.md).

The [Windows and Linux alpha](https://github.com/flujo-app/CommunityAI/releases/tag/v0.1.0-alpha.20260909.2)
is available for closed testing. See the [installation guide](docs/ALPHA_INSTALL.md).
This release adds automatic application downloads with **Restart to update**.
Users of the first release need to install this update once. Credits,
earnings, payments, and payouts are planned later and are not currently available.

## System requirements

**A dedicated GPU is optional.** For the current alpha, plan for **8 GB RAM and
20 GB free disk space** to install the app and use its small local model.
These are practical starting guidelines; we have not certified the lowest-end
CPU or smallest working RAM/VRAM configuration.

| Component | What you need |
| --- | --- |
| **CPU** | A 64-bit Intel or AMD processor (x86-64). **Four cores recommended**; CPU-only answers are slower. There is no validated minimum clock speed or processor generation yet. |
| **RAM** | **8 GB as a starting point; 16 GB recommended.** The small local model needs roughly 3.1 GB of available RAM, or free GPU memory if it runs on the GPU. Larger community models can need substantially more memory for the parts handled on your computer. |
| **GPU** | **Not required for CPU mode.** For GPU acceleration and sharing, use a CUDA-compatible NVIDIA GPU. An **RTX 2070 SUPER with 8 GB VRAM** has passed our sharing checks; lower VRAM capacities are not yet qualified. The current installers do not accelerate inference on AMD or Intel GPUs. |
| **Free disk space** | Start with **20 GB free**, preferably on an SSD. This allows room for the app, installer/temporary update files and the small local model. **Additional community-model downloads need extra space**, depending on the model and blocks you contribute. |
| **Operating system** | 64-bit **Windows 10 (1809 or later)/11**, or **Ubuntu 22.04+/Debian 12+** with a desktop environment. No native macOS or ARM installer is currently provided. |
| **Internet** | Required for initial model downloads and community inference/sharing. The small local model can run offline after downloading. |

The installed app occupies about **4.3 GB on Windows** or **5.2 GB on Linux**.
The small local model downloads another **1.8 GB**. The current Qwen3.8 27B
community checkpoint totals about **31 GB**, although clients and contributors
download only the files needed for their role. The small online installer still
downloads the complete runtime; it does not reduce installed disk usage.

For NVIDIA acceleration, install a driver compatible with the bundled CUDA 12.4
runtime. NVIDIA's CUDA 12.4 GA driver baseline is **551.61 on Windows** or
**550.54.14 on Linux**; use these versions or newer. You do not need to install
the CUDA Toolkit, Python or PyTorch separately.
[NVIDIA driver reference](https://docs.nvidia.com/cuda/archive/12.4.0/cuda-toolkit-release-notes/index.html).

The Windows OS floor follows the bundled [Qt runtime requirements](https://doc.qt.io/qt-6/windows.html).
See the [installation guide](docs/ALPHA_INSTALL.md) and
[recorded hardware checks](docs/evidence/gate14-20260907-final-resource-acceptance.md)
for details about the tested configurations.
