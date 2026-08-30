# Catalog bootstrap v1

Status: the strict sidecar consumer, desktop lifecycle integration, last-known-good
cache, best-effort-alpha publication-bundle contract, and fail-closed packaging handoff
are implemented. The threshold-one `public-alpha/catalog-v1` bundle publishes the
exact qualified first-rung manifests, pinned public mirror and seed, and self-verifying
first-install input. Gate 11 route operation passed through the generic product node;
packaged clean-install inference remains a separate gate.

`CatalogBootstrap v1` is the small, trusted release input that lets a clean desktop
installation find a model catalog and the public discovery network. It is application
configuration, not remotely supplied catalog metadata. The desktop passes its path to
the standalone node sidecar and never parses catalog signatures or model manifests
itself.

## Format

The document contains only:

- `schema_version`, currently `1`;
- the independent catalog `trust_root` defined by
  [`MODEL_CATALOG_V1.md`](MODEL_CATALOG_V1.md);
- one or more absolute HTTPS `catalog_mirrors` without credentials, queries, or
  fragments;
- one or more libp2p `initial_peers`; and
- the optional positive `max_loaded_models`, defaulting to one.

Unknown fields, duplicate JSON keys, unsafe catalog URLs, malformed seed addresses,
invalid trust roots, and symbolic-link inputs fail closed. Mirror hosts must be canonical,
control-free public DNS names or unscoped global IP literals on HTTPS port 443. Seeds must
be canonical direct IP/DNS TCP multiaddresses with a valid terminal Hivemind PeerID; an
`ip4` or `ip6` component must contain a matching-version IP literal, while `dns4` or
`dns6` must contain a DNS name rather than an IP literal. Local, private, link-local,
multicast, reserved, scoped, type-confused, numeric DNS lookalikes (including dotted
leading-zero IPv4 notation), and special-use hosts (including `.onion` and `home.arpa`)
are rejected before any fetch. Catalog URLs and public seed addresses are
intentionally outside ordinary `NodeConfig v1` until they have been verified and expanded
into exact model entries.

Create the release input after the offline root and catalog are ready:

```text
drift catalog bootstrap-config \
  --root catalog-root.json \
  --catalog-mirror https://mirror.example.com/communityai/catalog.signed.json \
  --initial-peer /dns4/bootstrap.communityai.flujo.com.co/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo \
  --output catalog-bootstrap.json
```

Before publication or bundling, verify the repository-controlled transport inputs:

```text
drift catalog publication-preflight catalog.signed.json \
  --bootstrap catalog-bootstrap.json \
  --manifest qwen3.5-2b-bfloat16-eager.json \
  --manifest gemma-4-e2b-it-bfloat16-eager.json \
  --output publication-preflight.json
```

The preflight verifies the signature threshold and expiry against the embedded root,
requires at least one public HTTPS mirror and one public seed, and requires distinct
hosts, addresses, and peer identities whenever additional endpoints are supplied. It
matches every catalog digest and manifested weight-byte total, rejects extra manifests
and selector collisions, and accepts the alpha policy minimum of one complete replica,
one route, and one surviving replica. Its report binds the exact canonical bootstrap
digest and always retains `complete_release_qualification=false`: endpoint strings do not
prove live DNS resolution, reachability, redundancy, independent operators, or real
qualification. Multiple independent mirrors, seeds, and routes remain post-alpha
hardening; worker soak and packaged inference remain separate gates.

Assemble the repository-auditable handoff as a deterministic directory:

```text
drift catalog publication-bundle catalog.signed.json \
  --bootstrap catalog-bootstrap.json \
  --manifest qwen3.5-2b-bfloat16-eager.json \
  --manifest gemma-4-e2b-it-bfloat16-eager.json \
  --output catalog-publication-bundle
```

The directory contains canonical `catalog-bootstrap.json`, `catalog.signed.json`,
`publication-preflight.json`, digest-addressed files below `manifests/`, and a
canonical `bundle.json` index with every member size and SHA-256 digest. Rebuilding
from the same parsed inputs produces the same names and bytes. Loading fails closed on
symlinks, missing or extra members, unsafe names, noncanonical or duplicate-key JSON,
unordered index entries, size or digest drift, signature/expiry failure, manifest
filename drift, or any mismatch among the catalog, bootstrap, manifests, preflight, and
index. `--force` can replace only an already valid bundle, preventing an output typo
from deleting an unrelated directory.

The release builder accepts and revalidates the complete bundle explicitly:

```text
python desktop/build_desktop.py \
  --publication-bundle catalog-publication-bundle
```

The builder validates the source before PyInstaller runs, then loads the actual staged
copy from the fixed `_internal/bootstrap/` onedir location and requires its complete
evidence to equal the pre-copy evidence. Only that packaged copy's bounded
catalog/bootstrap identity, canonical bundle-index digest, member count, and member
digests are recorded in `desktop-metrics.json`. Builds without release inputs remain
available. The complete public bundle is staged under the existing `bootstrap/`
packaged location, so the lifecycle consumer still reads
`bootstrap/catalog-bootstrap.json` while the signed catalog, exact manifests, report,
and audit index remain available beside it. On a missing
`node-config.json`, the GUI invokes the separately frozen node as:

```text
CommunityAI-Node bootstrap catalog-bootstrap.json \
  --data_dir <native node data directory> \
  --node_config <node-config.json>
```

The bootstrap process disables environment proxies, refuses redirects, bounds catalog
and manifest response bodies, requires HTTPS certificate validation, verifies catalog
threshold signatures, expiry, and rollback state, and accepts a manifest only when its
canonical digest exactly matches the signed catalog entry. Accepted manifests are
stored by digest. The rollback state is persisted before the generated configuration is
activated, and all files use same-directory temporary writes plus atomic replacement.

An existing `node-config.json` is validated and preserved without network access. If
remote mirrors are unavailable, a still-valid previously accepted catalog and its
content-addressed manifests can recreate a missing configuration. An expired cached
catalog cannot do so.

Bootstrap installs trust and configuration, not model weights. After configuration, the
generic node lazily materializes only the Hugging Face files required by a selected client
runtime or contribution block range, verifies them against the exact installed manifest,
and reuses a persistent cache. Catalog mirrors carry the signed catalog and manifests;
they are not model-weight mirrors. This boundary is fixed by
[ADR 0003](adr/0003-direct-manifested-artifact-delivery.md).

## Release gate

Do not bundle a placeholder root, an unsigned catalog, test-vector manifests, or a
private signing key. The published alpha bundle contains both exact qualified first-rung
manifests and a signed envelope that a fresh consumer fetched through its pinned HTTPS
mirror before recreating the two-model node configuration. The catalog declares
eligibility policy, not live capacity: `auto` still fails honestly when authenticated
coverage does not satisfy that policy. Gate 11 route operation has passed, while Gate 13
packaged inference remains a separate real-world gate. Catalog mirrors and seeds can be
replaced in a later signed application build; user-supplied seeds and independently
updateable discovery configuration remain milestone-6 work.
