# Catalog publisher key and emergency recovery

Updated 2026-09-06. This is the permanent locator for the **catalog signing key**.
It is separate from worker identities, desktop API credentials and installer signing.
Never put its contents into Git, release archives, logs, conversations or worker VMs.

## Current key and three backups

Public key ID: `sha256:9505d3ac8ec996d4b794bd43d09dd84447acc5da8a0bb1eac1be4e9c9a34b14f`.

| Copy | Location |
| --- | --- |
| Working publisher key | `%LOCALAPPDATA%\CommunityAI\publisher-keys\catalog-20260906\catalog-private.pem` |
| **Emergency online backup** | [Google Secret Manager: communityai-catalog-signer-20260906](https://console.cloud.google.com/security/secret-manager/secret/communityai-catalog-signer-20260906/versions?project=community-ai-506321), **version 1**, project `community-ai-506321` |
| Second backup | `G:\CommunityAI-Publisher-Backup\catalog-20260906\catalog-private.pem` |
| Third backup | Repository-local `.publisher-secrets/catalog-20260906/catalog-private.pem`, explicitly gitignored |

The online resource is
`projects/1091886728019/secrets/communityai-catalog-signer-20260906/versions/1`.
It has automatic replication, no expiry and no public IAM grant. The current project
IAM policy has one owner; workers were not granted access. Local directories have
Windows ACLs restricted to the publisher's Windows identity. G: and repository
copies stay in place by explicit owner request. The Google copy survives losing this PC.

The local non-secret registry is
`%LOCALAPPDATA%\CommunityAI\publisher-keys\active-catalog.json`; copies accompany
the G: and repository backups. It records the private paths, public identity and
pinned Google backup version. This document remains available if that registry is lost.

## Verify and recover

Authenticate Google Cloud CLI as the CommunityAI project owner. With the project
Python environment installed, run `python scripts/catalog_key_backup.py`. This
retrieves version 1 into memory, checks its public identity, signs and verifies a
challenge, and prints only the verification result. It never prints the key.

For emergency recovery, open the Google link above, select **version 1**, and save
the value as `catalog-private.pem` in an owner-restricted publisher directory.
Alternatively use `gcloud secrets versions access 1 --secret=communityai-catalog-signer-20260906
--project=community-ai-506321 --out-file=<protected-output-path>` as a single command.
Use `--out-file`; omitting it displays the private key in the terminal.
Verify the recovered key's public ID before signing. Do not generate another key
merely because the local working copy is missing.

`scripts/sign_catalog_candidate.py` loads the registered working key and verifies
the Google emergency copy before sealing a reviewed candidate. Sealing does not
publish it. `scripts/provision_catalog_signer.ps1` is for deliberate creation of a
new identity, not routine recovery.

## September 2026 replacement

The previous public key ID ended in `5ac8435`. Its original generation command was
found in the Gate 12 execution history: `.gatev-runtime/gate12-release/communityai-public-alpha-v1.pem`.
That directory no longer exists; no private material was recovered. Commit
`26be579` contains only its public root and signed catalog.

The replacement Qwen bundle uses catalog sequence 2 and a separate publication
directory, `public-alpha/catalog-qwen-v2`. Existing installations need the new
desktop build, whose bundled bootstrap explicitly authorizes replacing the exact
previous root digest. Network catalogs cannot change trust roots. The old
`catalog-v1` publication stays available for older clients; rollback watermarks and
user configuration survive the application migration.
