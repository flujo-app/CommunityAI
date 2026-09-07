# Windows code signing decision and eligibility inquiry

Status, September 7, 2026: **no enrollment, trusted certificate or signing
approval exists**. The owner prefers a free solution and would otherwise publish
as Mario Andreschak, an individual based in Colombia.

Later September 7 decision: publisher signing is **post-alpha**. Working unsigned
Windows setup and Debian installers are acceptable for the alpha, with explicit
unsigned labelling and checksums/provenance. Store submission follows signing.
The owner suggested Azure as a possible paid fallback, but its individual-country
restriction still excludes Colombia. Basic is currently $9.99 per month; deleting
the signing account does not affect certificates already used, although future
builds require signing again. Billing is by full month, not prorated. See
[Microsoft pricing](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-change-sku)
and [unenrollment/billing](https://learn.microsoft.com/en-us/azure/artifact-signing/faq).
No signing account or paid enrollment has been created.

With the owner's explicit authorization, an eligibility inquiry was sent from
their Gmail account to SignPath's published contact, `info@signpath.io`, on
September 7 at 12:08 Colombia time. Gmail's Sent folder confirmed the message was
sent; receipt or acceptance by SignPath has not been established.
Subject: **CommunityAI: free OSS signing eligibility for CUDA-enabled Windows
packages**. The inquiry requests the free program only; no paid service,
enrollment or signing terms were accepted.

## Recommendation

Apply to [SignPath Foundation](https://signpath.org/) for its free open-source
program. Its certificate identifies **SignPath Foundation** as publisher; it does
not create a certificate in the maintainer's personal name. Acceptance is at the
Foundation's discretion. Azure Artifact Signing Public Trust currently supports
individuals in the US and Canada, so it is not available for this individual
location. See [Microsoft's eligibility documentation](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart).

The material eligibility question is the CUDA-enabled PyTorch distribution:
our Windows runtime bundles NVIDIA CUDA, cuBLAS and cuDNN DLLs. SignPath's
[conditions](https://signpath.org/terms.html) exclude proprietary components
except System Libraries; the Foundation must decide whether this package fits.
The patched upstream Hivemind build and PyInstaller executables also need their
signing boundaries agreed. Do not relabel or re-sign third-party binaries as
CommunityAI-authored code to avoid these requirements.

Self-signing can test the pipeline but does not provide a publicly trusted
publisher for Windows downloads or satisfy the Store's trusted-signing requirement.
The existing catalog key authenticates model catalogs; it is separate from
Authenticode and must not be reused for it.

## Project information supplied in the inquiry

Project: **CommunityAI**

Repository and current project documentation:
<https://github.com/flujo-app/CommunityAI>

License: MIT; third-party runtime components retain their respective licenses.

Maintainer: Mario Andreschak, Colombia. The owner supplied the reply address;
it is omitted here to avoid adding personal contact details to public source.

Description: CommunityAI is a Windows/Linux desktop application for local AI
inference and opt-in community inference sharing. The desktop starts a separate
local node, exposes a loopback API, downloads model artifacts selected by signed
manifests, verifies their hashes, and displays contribution limits, block health,
peer metadata and acquisition progress. Sharing is off by default.

Build: public GitHub Actions workflow `.github/workflows/desktop.yaml`; Python
and PyInstaller build separate GUI and node executables. The Windows node uses a
patched Hivemind runtime and CUDA-enabled PyTorch. Inno Setup creates a per-user,
silent-capable installer. Current CI outputs are unsigned engineering artifacts;
the first public installer release and its permanent URL are still pending.

Eligibility inquiry: Can the Foundation sign our authored executables and Inno
installer/uninstaller while the package includes the NVIDIA runtime DLLs supplied
with PyTorch? Which upstream and PyInstaller artifact restrictions apply to this
build? What prior-release evidence is acceptable when the first public installer
is still pending? We offered the dependency/license inventory, workflow and
proposed signing policy before requesting signing approval.

Outstanding application inputs: eligibility response, permanent installer download
page, dependency/license inventory, confirmed MFA, named reviewer/approver roles,
and a reviewed privacy/code-signing policy. Do not claim sponsorship or display
the Foundation's attribution as an existing relationship before acceptance.

## Integration after acceptance

1. Agree which artifacts the provider can sign and record the approved policy.
   Set product/version metadata on the authored Windows executables.
2. Use verified GitHub build provenance, protected release inputs and the
   provider's required human signing approval. Signing credentials must not be
   available to pull-request builds.
3. Sign the approved payloads before packaging. Use Inno's `SignTool` hook and
   `SignedUninstaller=yes`; the checked-in Windows builder accepts a signing
   command and rejects an invalid resulting installer signature. The provider
   integration is pending; this generic hook alone is not a working SignPath
   enrollment or end-to-end signing pipeline.
4. Verify the final installer, uninstaller and required PE payload signatures;
   generate final checksums after signing. Run the real installer lifecycle and
   follow the Store's payload-signing and offline/silent installation rules.

The Foundation requires a prior release in the intended format and verifiable
project reputation. Build/test artifacts help prepare an application; they do
not guarantee eligibility. If declined, agree on a paid individual signing
provider available in Colombia or revisit the distribution package with the
owner; no purchase is authorized by this document.
