import hashlib
import json
import shutil
import sys
import time

import pytest

from drift.catalog_release import (
    catalog_bootstrap_digest,
    load_catalog_publication_bundle,
    load_catalog_publication_preflight_report,
    verify_catalog_publication_bundle,
    verify_catalog_publication_preflight_report,
    write_catalog_publication_bundle,
)
from drift.cli import run_catalog
from drift.model_catalog import CATALOG_SCHEMA_VERSION, CatalogSigningKey, ModelCatalog, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapError


def _manifest(name: str, alias: str) -> ModelManifest:
    source = ModelManifest.load("tests/data/model_manifest_v1_vector.json").to_dict()
    source["name"] = name
    source["aliases"] = [alias]
    return ModelManifest.from_dict(source)


def _documents(
    *,
    mirror_urls: list[str] | None = None,
    initial_peers: list[str] | None = None,
    weight_delta: int = 0,
    shared_alias: bool = False,
):
    primary = _manifest("Primary Test", "shared" if shared_alias else "primary-test")
    standby = _manifest("Standby Test", "shared" if shared_alias else "standby-test")
    now = time.time()
    models = []
    for role, manifest in (("primary", primary), ("standby", standby)):
        weight_bytes = sum(artifact.size for artifact in manifest.artifacts if artifact.role == "weight")
        models.append(
            {
                "manifest_digest": manifest.digest_id,
                "manifest_urls": [f"https://models.example/{manifest.digest}.json"],
                "rung": "1-2b",
                "role": role,
                "total_parameters": 1_000_000_000,
                "active_parameters": 1_000_000_000,
                "weight_bytes": weight_bytes + (weight_delta if role == "primary" else 0),
            }
        )
    catalog = ModelCatalog.from_dict(
        {
            "catalog_id": "communityai-publication-test",
            "sequence": 1,
            "issued_at_ms": int((now - 60) * 1000),
            "expires_at_ms": int((now + 3600) * 1000),
            "rungs": [
                {
                    "id": "1-2b",
                    "order": 1,
                    "minimum_replicas": 2,
                    "minimum_independent_routes": 2,
                    "minimum_surviving_replicas": 1,
                    "minimum_soak_seconds": 60,
                    "maximum_observation_age_seconds": 30,
                    "maximum_p95_first_token_ms": 2_000,
                    "minimum_tokens_per_minute": 60,
                }
            ],
            "models": models,
        }
    )
    key = CatalogSigningKey.generate()
    envelope = SignedModelCatalog(CATALOG_SCHEMA_VERSION, catalog, ()).add_signature(key)
    bootstrap = CatalogBootstrapConfig.from_dict(
        {
            "schema_version": 1,
            "trust_root": {
                "schema_version": 1,
                "catalog_id": catalog.catalog_id,
                "threshold": 1,
                "keys": [key.trusted_key.to_dict()],
            },
            "catalog_mirrors": mirror_urls
            or [
                "https://catalog-one.example/catalog.signed.json",
                "https://catalog-two.example/catalog.signed.json",
            ],
            "initial_peers": initial_peers
            or [
                "/dns4/seed-one.example/tcp/31337/p2p/QmSeedOne",
                "/dns4/seed-two.example/tcp/31337/p2p/QmSeedTwo",
            ],
            "max_loaded_models": 1,
        }
    )
    return bootstrap, envelope, (primary, standby)


def test_publication_preflight_matches_signed_transport_and_exact_manifests():
    bootstrap, envelope, manifests = _documents()

    report = verify_catalog_publication_bundle(bootstrap, envelope, manifests)

    assert report["result"] == "passed"
    assert report["catalog_digest"] == envelope.signed.digest
    assert report["bootstrap_digest"] == catalog_bootstrap_digest(bootstrap)
    assert report["catalog_mirror_count"] == 2
    assert report["distinct_seed_host_count"] == 2
    assert report["distinct_seed_address_count"] == 2
    assert report["distinct_seed_identity_count"] == 2
    assert report["model_count"] == 2
    assert report["complete_release_qualification"] is False
    assert "public-worker route redundancy and soak" in report["not_covered"]


@pytest.mark.parametrize(
    "documents, manifests_selector, message",
    [
        (
            lambda: _documents(mirror_urls=["https://catalog-one.example/catalog.json"]),
            lambda manifests: manifests,
            "at least two catalog mirrors",
        ),
        (
            lambda: _documents(
                mirror_urls=[
                    "https://catalog.example/one/catalog.json",
                    "https://catalog.example/two/catalog.json",
                ]
            ),
            lambda manifests: manifests,
            "distinct network hosts",
        ),
        (
            lambda: _documents(initial_peers=["/dns4/seed-one.example/tcp/31337/p2p/QmSeedOne"]),
            lambda manifests: manifests,
            "at least one additional seed",
        ),
        (
            lambda: _documents(
                initial_peers=[
                    "/dns4/seed.example/tcp/31337/p2p/QmSeedOne",
                    "/dns4/seed.example/tcp/31338/p2p/QmSeedTwo",
                ]
            ),
            lambda manifests: manifests,
            "distinct network hosts",
        ),
        (
            lambda: _documents(
                initial_peers=[
                    "/dns4/seed-one.example/tcp/31337/p2p/QmSame",
                    "/dns4/seed-two.example/tcp/31337/p2p/QmSame",
                ]
            ),
            lambda manifests: manifests,
            "distinct libp2p peer identities",
        ),
        (
            lambda: _documents(weight_delta=1),
            lambda manifests: manifests,
            "weight_bytes",
        ),
        (
            lambda: _documents(shared_alias=True),
            lambda manifests: manifests,
            "reuse model selector",
        ),
        (
            _documents,
            lambda manifests: manifests[:1],
            "missing catalog manifest",
        ),
        (
            _documents,
            lambda manifests: (*manifests, manifests[0]),
            "duplicate manifest",
        ),
    ],
)
def test_publication_preflight_fails_closed(documents, manifests_selector, message):
    bootstrap, envelope, manifests = documents()

    with pytest.raises(CatalogBootstrapError, match=message):
        verify_catalog_publication_bundle(bootstrap, envelope, manifests_selector(manifests))


def test_publication_preflight_cli_writes_partial_machine_readable_report(tmp_path, monkeypatch, capsys):
    bootstrap, envelope, manifests = _documents()
    bootstrap_path = tmp_path / "catalog-bootstrap.json"
    catalog_path = tmp_path / "catalog.signed.json"
    manifest_paths = [tmp_path / f"manifest-{index}.json" for index in range(len(manifests))]
    output_path = tmp_path / "publication-preflight.json"
    bootstrap_path.write_text(json.dumps(bootstrap.to_dict()), encoding="utf-8")
    catalog_path.write_text(json.dumps(envelope.to_dict()), encoding="utf-8")
    for path, manifest in zip(manifest_paths, manifests):
        path.write_text(manifest.canonical_json(), encoding="utf-8")

    argv = [
        "drift catalog",
        "publication-preflight",
        str(catalog_path),
        "--bootstrap",
        str(bootstrap_path),
    ]
    for path in manifest_paths:
        argv.extend(("--manifest", str(path)))
    argv.extend(("--output", str(output_path)))
    monkeypatch.setattr(sys, "argv", argv)

    run_catalog.main()

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["result"] == "passed"
    assert report["complete_release_qualification"] is False
    assert "external release gates remain open" in capsys.readouterr().out


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("schema_version", 2, "schema version"),
        ("schema_version", 1.0, "schema version"),
        ("scope", "other", "unexpected scope"),
        ("result", "failed", "did not pass"),
        ("complete_release_qualification", True, "incomplete release qualification"),
        ("catalog_id", "other-catalog", "catalog_id"),
        ("catalog_sequence", 0, "catalog_sequence"),
        ("catalog_digest", "sha256:not-a-digest", "catalog_digest"),
        ("bootstrap_digest", "sha256:" + "0" * 64, "exact bootstrap"),
    ],
)
def test_publication_preflight_report_validation_fails_closed(field, value, message):
    bootstrap, envelope, manifests = _documents()
    report = verify_catalog_publication_bundle(bootstrap, envelope, manifests)
    report[field] = value

    with pytest.raises(CatalogBootstrapError, match=message):
        verify_catalog_publication_preflight_report(bootstrap, report)


def test_publication_preflight_report_loader_rejects_duplicate_keys(tmp_path):
    bootstrap, envelope, manifests = _documents()
    report = verify_catalog_publication_bundle(bootstrap, envelope, manifests)
    source = json.dumps(report).replace('"result": "passed"', '"result": "passed", "result": "passed"')
    report_path = tmp_path / "publication-preflight.json"
    report_path.write_text(source, encoding="utf-8")

    with pytest.raises(CatalogBootstrapError, match="duplicate object key"):
        load_catalog_publication_preflight_report(bootstrap, report_path)


def test_publication_preflight_report_loader_returns_bounded_release_evidence(tmp_path):
    bootstrap, envelope, manifests = _documents()
    report = verify_catalog_publication_bundle(bootstrap, envelope, manifests)
    report_path = tmp_path / "publication-preflight.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    evidence = load_catalog_publication_preflight_report(bootstrap, report_path)

    assert evidence == {
        "schema_version": 1,
        "scope": "catalog-publication-transport-preflight",
        "result": "passed",
        "catalog_id": envelope.signed.catalog_id,
        "catalog_sequence": envelope.signed.sequence,
        "catalog_digest": envelope.signed.digest,
        "bootstrap_digest": catalog_bootstrap_digest(bootstrap),
        "complete_release_qualification": False,
    }


def _bundle_snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_publication_bundle_is_deterministic_and_self_verifying(tmp_path):
    bootstrap, envelope, manifests = _documents()
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_index = write_catalog_publication_bundle(first, bootstrap, envelope, manifests)
    second_index = write_catalog_publication_bundle(second, bootstrap, envelope, tuple(reversed(manifests)))

    assert first_index == second_index
    assert _bundle_snapshot(first) == _bundle_snapshot(second)
    assert load_catalog_publication_bundle(first) == first_index
    assert first_index["complete_release_qualification"] is False
    paths = [entry["path"] for entry in first_index["files"]]
    assert paths == sorted(paths)
    assert paths == [
        "catalog-bootstrap.json",
        "catalog.signed.json",
        *sorted(f"manifests/{manifest.digest}.json" for manifest in manifests),
        "publication-preflight.json",
    ]
    report = json.loads((first / "publication-preflight.json").read_text(encoding="utf-8"))
    assert report["complete_release_qualification"] is False


def test_publication_bundle_rejects_tampered_and_extra_members(tmp_path):
    bootstrap, envelope, manifests = _documents()
    bundle = tmp_path / "bundle"
    write_catalog_publication_bundle(bundle, bootstrap, envelope, manifests)

    catalog_path = bundle / "catalog.signed.json"
    catalog_path.write_bytes(catalog_path.read_bytes() + b" ")

    with pytest.raises(CatalogBootstrapError, match="member .* mismatch"):
        load_catalog_publication_bundle(bundle)

    extra_bundle = tmp_path / "extra-bundle"
    write_catalog_publication_bundle(extra_bundle, bootstrap, envelope, manifests)
    (extra_bundle / "unexpected.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(CatalogBootstrapError, match="members do not match"):
        load_catalog_publication_bundle(extra_bundle)


def test_publication_bundle_rejects_digest_mismatch_for_every_indexed_member(tmp_path):
    bootstrap, envelope, manifests = _documents()
    source = tmp_path / "source"
    index = write_catalog_publication_bundle(source, bootstrap, envelope, manifests)

    for position, entry in enumerate(index["files"]):
        case = tmp_path / f"digest-{position}"
        shutil.copytree(source, case)
        member = case.joinpath(*entry["path"].split("/"))
        raw = member.read_bytes()
        member.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])

        with pytest.raises(CatalogBootstrapError, match=f"member digest mismatch for {entry['path']}"):
            load_catalog_publication_bundle(case)


def test_publication_bundle_rejects_noncanonical_json_for_every_document(tmp_path):
    bootstrap, envelope, manifests = _documents()
    source = tmp_path / "source"
    index = write_catalog_publication_bundle(source, bootstrap, envelope, manifests)
    members = ["bundle.json", *(entry["path"] for entry in index["files"])]

    for position, relative_path in enumerate(members):
        case = tmp_path / f"noncanonical-{position}"
        shutil.copytree(source, case)
        member = case.joinpath(*relative_path.split("/"))
        value = json.loads(member.read_text(encoding="utf-8"))
        rendered = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        member.write_bytes(rendered)
        if relative_path != "bundle.json":
            case_index_path = case / "bundle.json"
            case_index = json.loads(case_index_path.read_text(encoding="utf-8"))
            entry = next(item for item in case_index["files"] if item["path"] == relative_path)
            entry["size"] = len(rendered)
            entry["sha256"] = f"sha256:{hashlib.sha256(rendered).hexdigest()}"
            case_index_path.write_text(
                json.dumps(
                    case_index,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

        with pytest.raises(CatalogBootstrapError, match="not canonical JSON"):
            load_catalog_publication_bundle(case)


def test_publication_bundle_rejects_symlinked_root_and_member(tmp_path):
    bootstrap, envelope, manifests = _documents()
    source = tmp_path / "source"
    write_catalog_publication_bundle(source, bootstrap, envelope, manifests)
    root_link = tmp_path / "root-link"
    try:
        root_link.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(CatalogBootstrapError, match="missing or unsafe"):
        load_catalog_publication_bundle(root_link)

    member_bundle = tmp_path / "member-link"
    shutil.copytree(source, member_bundle)
    member = member_bundle / "catalog.signed.json"
    target = tmp_path / "catalog-target.json"
    member.replace(target)
    member.symlink_to(target)

    with pytest.raises(CatalogBootstrapError, match="unsafe symbolic link|missing or unsafe"):
        load_catalog_publication_bundle(member_bundle)


def test_publication_bundle_force_never_replaces_an_unrelated_directory(tmp_path):
    bootstrap, envelope, manifests = _documents()
    output = tmp_path / "not-a-bundle"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(CatalogBootstrapError, match="members do not match|missing"):
        write_catalog_publication_bundle(output, bootstrap, envelope, manifests, force=True)

    assert marker.read_text(encoding="utf-8") == "user data"


def test_publication_bundle_cli_creates_verified_directory(tmp_path, monkeypatch, capsys):
    bootstrap, envelope, manifests = _documents()
    bootstrap_path = tmp_path / "catalog-bootstrap.json"
    catalog_path = tmp_path / "catalog.signed.json"
    manifest_paths = [tmp_path / f"manifest-{index}.json" for index in range(len(manifests))]
    output_path = tmp_path / "publication-bundle"
    bootstrap_path.write_text(json.dumps(bootstrap.to_dict()), encoding="utf-8")
    catalog_path.write_text(json.dumps(envelope.to_dict()), encoding="utf-8")
    for path, manifest in zip(manifest_paths, manifests):
        path.write_text(manifest.canonical_json(), encoding="utf-8")

    argv = [
        "drift catalog",
        "publication-bundle",
        str(catalog_path),
        "--bootstrap",
        str(bootstrap_path),
    ]
    for path in manifest_paths:
        argv.extend(("--manifest", str(path)))
    argv.extend(("--output", str(output_path)))
    monkeypatch.setattr(sys, "argv", argv)

    run_catalog.main()

    index = load_catalog_publication_bundle(output_path)
    assert index["catalog_digest"] == envelope.signed.digest
    assert index["complete_release_qualification"] is False
    assert "external qualification and publication gates remain open" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        run_catalog.main()
    assert "Refusing to overwrite" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", [*argv, "--force"])
    run_catalog.main()
    assert load_catalog_publication_bundle(output_path) == index
