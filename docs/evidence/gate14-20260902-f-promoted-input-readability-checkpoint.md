# Gate 14 promoted-input readability checkpoint

- Date: 2026-09-02 (America/Bogota)
- Gate: 14, automatic contribution and resource-control hardware checks
- Result: software checkpoint passed; Gate 14 remains in progress
- Cloud spend: USD 0
- Provider resources created: none
- Production model bytes downloaded: none

## Blocker closed

Independent bootstrap-boundary review found that promotion made the final lifecycle configuration and cache-materialization record unreadable by the ordinary identity that must execute the durable host job. POSIX promotion used root-owned mode `0600`; the Windows protected DACL granted access only to SYSTEM and Administrators. Both forms preserved controller ownership but prevented `gate14` on Linux or the limited Windows desktop user from loading the promoted inputs.

Promotion now preserves the privilege split while allowing the required read path:

- POSIX files are root-owned mode `0644`: all identities may read, but only the controller owner may modify them.
- Windows files retain a protected DACL with SYSTEM and Administrators full control and Authenticated Users generic read: the ordinary job identity can read, but receives no write, delete, owner-change, or DACL-change grant.
- The existing lifecycle verifier still checks structural controller ownership, and the ordinary lifecycle path still performs the independent write-denial probes.

## Verification

- Focused cache/lifecycle suite: `97 passed`
- Complete Gate 14 suite: `254 passed`
- Black, isort, py_compile, and diff checks: passed
- Independent adversarial review: passed; it confirmed no ACL or deletion blocker and reproduced 97 focused tests plus the targeted protection subset

Verified working-tree SHA-256 values:

- `scripts/gate14_cache_materializer.py`: `0e79c4acf8c3cb39af4d7e5787636187e733c8ffd2cee1cdf985b7965077a0c7`
- `tests/test_gate14_cache_materializer.py`: `3314437c0b0fe7062cf84454501b7db90b75c2bccd8e802ab75e42f13eac249b`

## Remaining Gate 14 work

Build and verify the source-bound Windows/Linux host bootstrap around the corrected readable/nonwritable staging contract. The bootstrap must prepare exact package/audit/source inputs, run ordinary materialization then privileged promotion, start the ordinary durable host job, transport the checkpoint/challenge/evidence without broadening write access, and prove exact cleanup before any paid host is created. Matching production desktop artifacts for the eventual integration source are also still required.
