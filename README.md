# CommunityAI

[![Version](https://img.shields.io/badge/version-2.3.0.dev2-6d4aff)](https://github.com/flujo-app/CommunityAI)
[![Tests](https://github.com/flujo-app/CommunityAI/actions/workflows/run-tests.yaml/badge.svg?branch=main)](https://github.com/flujo-app/CommunityAI/actions/workflows/run-tests.yaml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e.svg)](LICENSE)

**AI powered by people.**

<img width="1202" height="832" alt="image" src="https://github.com/user-attachments/assets/14bfe3e2-4d47-4beb-9230-7b22cc962838" />

## How it works

Community-AI is a shared Large-Language-Model, by the people, for the people.

1. Start the app.
2. Connect an OpenAI-compatible client to the local endpoint.
3. Use public community inference and optionally share compute within your limits.

Community-AI takes care of everything else.

"Home" Screen:
<img width="1202" height="832" alt="image" src="https://github.com/user-attachments/assets/0e02de2b-88f0-4af0-8fe6-79b2ed9a5979" />

"Models" Screen:
<img width="1202" height="832" alt="image" src="https://github.com/user-attachments/assets/7a3a4d73-cc0a-4148-af8c-082c3788480e" />

"Sharing" Screen:
<img width="1202" height="832" alt="image" src="https://github.com/user-attachments/assets/ef20f95e-a199-4058-adf9-d9782de5a005" />

## System requirements

A dedicated GPU is optional. These are practical starting guidelines for the
alpha; the lowest supported hardware configuration has not been certified.

| Component | What you need |
| --- | --- |
| **CPU** | 64-bit Intel or AMD (x86-64). Four cores recommended. |
| **RAM** | 8 GB as a starting point; 16 GB recommended. Sharing needs extra memory for the model parts you contribute. |
| **GPU** | Optional. NVIDIA CUDA is supported for acceleration and sharing; an RTX 2070 SUPER with 8 GB VRAM has been tested. AMD and Intel GPU acceleration is not included. |
| **Disk space** | Start with 20 GB free, preferably on an SSD. The app uses about 4.3 GB on Windows or 5.2 GB on Linux; the small local model adds about 1.8 GB. Sharing requires additional space. |
| **Operating system** | 64-bit Windows 10 (1809+)/11, Ubuntu 22.04+ or Debian 12+ with a desktop environment. |
| **Internet** | Required for community inference and initial downloads. The small local model works offline after downloading. |

When the community model is available, your app sends text to peers without
downloading its weights or processing model layers locally. The small local
fallback remains available when the mesh cannot answer. Sharing your GPU is optional.


[Download CommunityAI for Windows or Linux](https://github.com/flujo-app/CommunityAI/releases/tag/v0.1.0-alpha.20260909.3)
— available for closed alpha testing. Already using the app's updater? Check for
updates in the sidebar, then choose **Restart to update** when the download finishes.

**Latest release: 0.1.0-alpha.20260909.3**

| Platform | Online installer | Offline installer |
| --- | --- | --- |
| Windows | [Download setup](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.3/communityai-0.1.0-alpha.20260909.3-windows-online-setup.exe) | [Download full setup](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.3/communityai-0.1.0-alpha.20260909.3-windows-setup.exe) |
| Ubuntu/Debian | [Download installer (Python)](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.3/communityai-0.1.0-alpha.20260909.3-linux-online.py) | [Download .deb](https://pub-1f8764bf149e4e269735e087a4808e4c.r2.dev/alpha/20260909.3/communityai_0.1.0~alpha.20260909.3_amd64.deb) |

The online installer downloads and verifies the full package during setup.

For NVIDIA use, install a CUDA 12.4-compatible driver (551.61+ on Windows,
550.54.14+ on Linux). Python, PyTorch and the CUDA runtime are included.
See the [installation guide](https://github.com/flujo-app/CommunityAI/blob/codex/gate14-20260902-b/docs/ALPHA_INSTALL.md)
for setup, updates and the current alpha limitations.

## Important note on privacy and security:
**Do NOT use CommunityAI for confidential/private data**
**The peer handling your text can read it. The encryption protects on the network but a peer must actually open the message to process it. We cannot guarantee that another person’s computer won’t record it, or that a malicious peer will answer honestly.**

Nonetheless, this is what we do right now to make it as secure as possible:
- Messages between computers are encrypted
- Each computer proves it owns the identity it advertises (like an impersonation check)
- The files you download (Installer, Model-Data, etc.) must match our (preapproved) hashes.
- The updater checks our digital signature and the downloaded package before installing.
- We limit request sizes, simultaneous work and waiting times to make it harder for someone to overwhelm the network.
- All public (non-encrypted) status information is just things like available blocks and rough activity counts.


Again, don't use it for confident/company data!
