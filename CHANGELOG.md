# Changelog

All notable user-visible changes to CommunityAI are documented here. Detailed engineering
and qualification evidence remains in `docs/REVIVAL_TEST_RESULTS.md`.

## Unreleased

### Added

- Persistent multi-model local node with authenticated OpenAI-compatible inference and a
  separately authorized control API.
- PySide desktop foundation with native credential ownership, model and route status,
  client-key management, worker controls, single-instance ownership, and login startup.
- Exact content-addressed model manifests, verified artifacts, signed worker identities,
  encrypted manifested transport, bounded failure recovery, and signed catalog/bootstrap
  formats.
- Qwen3.5 2B and Gemma 4 E2B first-rung candidate manifests with Windows CPU parity and
  local selected-worker recovery evidence.
- Authoritative opt-in contribution policies for model admission, storage, scheduling,
  VRAM, bandwidth, power, and bounded pause behavior.
- Provider-neutral separate-machine qualification controller and a Fly Machines adapter
  with selected-worker hard kill and fail-closed resource cleanup.

### Changed

- Defined the first release as a Windows/Linux public inference alpha. macOS and all
  credit/payment features are explicitly deferred and will not be advertised as available.
- Model qualification now treats exact Windows/Linux CPU/CUDA coverage as the strict
  public-alpha matrix; macOS CPU/MPS evidence is collected as a separate deferred gate.
- Fly qualification now reuses the existing native `flyctl` login by default instead of
  requiring a manually supplied token environment variable.
- Model qualification dispatch no longer requires a persistent GitHub runner-inventory
  administration token.
- Production desktop CI now packages the public alpha on Windows and Linux only; macOS
  packaging remains explicitly deferred.
- Root product messaging now describes public inference and optional compute sharing
  without claiming that credits, earnings, or spending already exist.

### Security

- Public model loading fails closed on manifest, artifact, execution-profile, signed
  announcement, PeerID, transport, revocation, expiry, and rollback mismatches.
- Desktop control credentials remain separate from inference keys and live in native OS
  credential storage when the desktop owns the node.
- Temporary provider qualification resources are bound to exact run metadata and cannot
  produce a passing report unless complete cleanup is proven.

### Not included in the first public alpha

- Credits, receipts, balances, payments, earnings, payouts, or a compute marketplace.
- macOS support.
- Stable-service or production-SLO claims.
