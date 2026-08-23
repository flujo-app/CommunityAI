# Catalog bootstrap v1

Status: the strict sidecar consumer, desktop lifecycle integration, last-known-good
cache, and packaging hook are implemented. A production bootstrap file is deliberately
not checked in until the first model manifests have completed qualification and the
corresponding catalog has been signed and published.

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
invalid trust roots, and symbolic-link inputs fail closed. Catalog URLs and public seed
addresses are intentionally outside ordinary `NodeConfig v1` until they have been
verified and expanded into exact model entries.

Create the release input after the offline root and catalog are ready:

```text
drift catalog bootstrap-config \
  --root catalog-root.json \
  --catalog-mirror https://mirror-one.example/communityai/catalog.signed.json \
  --catalog-mirror https://mirror-two.example/communityai/catalog.signed.json \
  --initial-peer /dns4/bootstrap.communityai.flujo.com.co/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo \
  --output catalog-bootstrap.json
```

The release builder accepts that file explicitly:

```text
python desktop/build_desktop.py --bootstrap-config catalog-bootstrap.json
```

It is staged into the GUI bundle as public read-only configuration. On a missing
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

## Release gate

Do not bundle a placeholder root, an unsigned catalog, test-vector manifests, or a
private signing key. The first production bootstrap becomes eligible only after both
options in its first rung have exact qualified manifests, the signed envelope is
available through HTTPS mirrors, and real workers provide the catalog's claimed usable
routes. Catalog mirrors and seeds can be replaced in a later signed application build;
user-supplied seeds and independently updateable discovery configuration remain
milestone-6 work.
