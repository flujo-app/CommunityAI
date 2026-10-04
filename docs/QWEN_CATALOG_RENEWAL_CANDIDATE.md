# Qwen catalog sequence-3 review candidate

The committed Qwen sequence-2 catalog expired on 2026-09-28. Its signature and
publication bundle can still be checked at its signed issue time as historical
evidence. This does **not** make sequence 2 valid for current clients.

After the owner reviews the model roster, rung policies, public artifact sources,
mirror and seed availability, and chooses an issue time and expiry, prepare an
unsigned candidate from the exact sequence-2 bundle:

```powershell
python scripts/prepare_qwen_catalog_renewal_candidate.py `
  public-alpha/catalog-qwen-v2 `
  --expected-current-catalog-digest sha256:13c83590b7b47c86ae676c6e1a0e5277228fabbd2ba90c81babb6eaf430e5a80 `
  --expected-trust-root-digest sha256:7cf438b5e45335741b644fd532c74cfbe57b20c310144d6aa58f34ba712be388 `
  --issued-at $reviewedIssuedAtUtc `
  --expires-at $reviewedExpiresAtUtc `
  --output $newCandidateDirectory
```

Set the three PowerShell variables to the reviewed values first. Times use
`YYYY-MM-DDTHH:MM:SSZ`; the issue time must be current within the catalog's
five-minute clock tolerance, expiry must be in the future, and the interval cannot
exceed 180 days. The output directory must not already exist. The tool requires
the known sequence-2 catalog and trust-root digests, checks the complete indexed
publication and its Ed25519 signature, and copies the bootstrap and manifests
byte-for-byte. It changes only the catalog sequence and reviewed timestamps.

The output contains `catalog.unsigned.json` with an empty signature array,
`catalog-bootstrap.json`, the unchanged `manifests/`, and `review.json`. There is
no signed catalog or publication bundle. Inspect the unsigned catalog and review
metadata before any later sealing decision. The existing sealing script rewrites
manifest and mirror URLs for a chosen publication base; review that final payload
and digest separately. Qualification and publication remain separate gates.
