"""Prepare an isolated signed canary channel and observe ordinary packaged refresh.

Preparation and phase changes write only a new/local bundle. Publishing that
bundle to an existing HTTPS path is a separate operator action. No production key,
catalog, native credential, worker, GUI or cloud resource is modified here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from drift.model_catalog import CatalogSigningKey, ModelCatalog, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapInstaller
from drift.node.config import NodeConfig

PHASES = ("baseline", "withdrawal", "restore")
MAX_SECONDS = 900
ROOT = Path(__file__).resolve().parents[1]


class ChannelError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ChannelError(code)


def encode(value):
    return (json.dumps(value, indent=2, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_bundle(path):
    require(path.is_dir() and not path.is_symlink(), "unsafe_bundle")
    index = json.loads((path / "bundle.json").read_text())
    require(index["schema_version"] == 1 and index["catalog_id"].startswith("communityai-canary-"), "not_canary_bundle")
    bootstrap = CatalogBootstrapConfig.load(path / "catalog-bootstrap.json")
    require(bootstrap.trust_root.catalog_id == index["catalog_id"], "canary_root_mismatch")
    require(bootstrap.trust_root_digest == index["trust_root_digest"], "canary_root_digest_mismatch")
    for phase in PHASES:
        data = (path / "phases" / f"{phase}.signed.json").read_bytes()
        require(sha(data) == index["phases"][phase]["sha256"], "phase_digest_mismatch")
        catalog = SignedModelCatalog.from_json(data.decode()).verify(bootstrap.trust_root)
        require(catalog.sequence == index["phases"][phase]["sequence"], "phase_sequence_mismatch")
    return index, bootstrap


def prepare(args, *, now=None):
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.run_id) is not None, "run_id")
    require(args.base_url.endswith("/"), "base_url_requires_trailing_slash")
    original_bootstrap = CatalogBootstrapConfig.load(args.release / "catalog-bootstrap.json")
    original = SignedModelCatalog.from_json((args.release / "catalog.signed.json").read_text()).verify(
        original_bootstrap.trust_root
    )
    local = tuple(model for model in original.models if model.execution == "local")
    remote = tuple(model for model in original.models if model.execution == "distributed")
    require(len(local) == len(remote) == 1, "requires_one_local_and_one_community_model")
    key = CatalogSigningKey.generate()  # Kept only in memory; all three phases are signed now.
    catalog_id = "communityai-canary-" + args.run_id
    bootstrap = CatalogBootstrapConfig.from_dict(
        {
            "schema_version": 1,
            "trust_root": {
                "schema_version": 1,
                "catalog_id": catalog_id,
                "threshold": 1,
                "keys": [key.trusted_key.to_dict()],
            },
            "catalog_mirrors": [args.base_url + "catalog.signed.json"],
            "initial_peers": [args.initial_peer],
            "max_loaded_models": original_bootstrap.max_loaded_models,
        }
    )
    models = tuple(
        replace(
            model,
            manifest_urls=(args.base_url + "manifests/" + model.manifest_digest.removeprefix("sha256:") + ".json",),
        )
        for model in original.models
    )
    issued = int((time.time() if now is None else now) * 1000) - 1000
    baseline = replace(
        original,
        catalog_id=catalog_id,
        sequence=1,
        issued_at_ms=issued,
        expires_at_ms=issued + 2 * 60 * 60 * 1000,
        models=models,
    )
    local_model = next(model for model in models if model.execution == "local")
    withdrawal = replace(
        baseline,
        sequence=2,
        models=(local_model,),
        rungs=tuple(rung for rung in baseline.rungs if rung.rung_id == local_model.rung_id),
    )
    restore = replace(baseline, sequence=3)
    for catalog in (baseline, withdrawal, restore):
        ModelCatalog.from_dict(catalog.to_dict())
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "phases").mkdir()
    channel = args.output / "channel"
    (channel / "manifests").mkdir(parents=True)
    manifests = {}
    for model in models:
        name = model.manifest_digest.removeprefix("sha256:") + ".json"
        manifest = ModelManifest.load(args.release / "manifests" / name)
        require(manifest.digest_id == model.manifest_digest, "manifest_digest_mismatch")
        rendered = manifest.canonical_json() + "\n"
        (channel / "manifests" / name).write_text(rendered, encoding="utf-8")
        manifests[model.manifest_urls[0]] = rendered
    index = {
        "schema_version": 1,
        "scope": "isolated-catalog-canary-channel",
        "complete_gate16": False,
        "catalog_id": catalog_id,
        "trust_root_digest": bootstrap.trust_root_digest,
        "original_catalog_digest": original.digest,
        "local_manifest_digest": local_model.manifest_digest,
        "community_manifest_digest": remote[0].manifest_digest,
        "phases": {},
        "expires_at_ms": baseline.expires_at_ms,
        "private_signing_key_retained": False,
        "published": False,
    }
    for phase, catalog in zip(PHASES, (baseline, withdrawal, restore)):
        envelope = SignedModelCatalog(1, catalog, ()).add_signature(key)
        data = encode(envelope.to_dict())
        (args.output / "phases" / f"{phase}.signed.json").write_bytes(data)
        index["phases"][phase] = {"sequence": catalog.sequence, "sha256": sha(data), "catalog_digest": catalog.digest}
    (args.output / "catalog-bootstrap.json").write_bytes(encode(bootstrap.to_dict()))
    (args.output / "bundle.json").write_bytes(encode(index))
    (channel / "catalog.signed.json").write_bytes((args.output / "phases/baseline.signed.json").read_bytes())

    # Prepare only this new private state offline. The frozen node will consume
    # subsequent phases through its real HTTPS refresh service. Keeping local-only
    # and sharing disabled prevents autonomous model probes/downloads on launch.
    state = args.output / "private-node"
    installer = CatalogBootstrapInstaller(
        bootstrap,
        data_dir=state,
        config_path=state / "node-config.json",
        now=(issued + 1000) / 1000,
        fetch_text=lambda url, maximum: (channel / "catalog.signed.json").read_text()
        if url in bootstrap.catalog_mirrors
        else manifests[url],
    )
    installer.install()
    config = json.loads((state / "node-config.json").read_text())
    config["inference_mode"] = "local_only"
    config["contribution_policy"]["sharing_enabled"] = False
    (state / "node-config.json").write_bytes(encode(config))
    return {"result": "prepared", "complete_gate16": False, "catalog_id": catalog_id, "published": False}


def advance(args):
    index, bootstrap = load_bundle(args.bundle)
    target = args.bundle / "channel/catalog.signed.json"
    require(target.is_file() and not target.is_symlink(), "unsafe_active_catalog")
    current_bytes = target.read_bytes()
    current = SignedModelCatalog.from_json(current_bytes.decode()).verify(bootstrap.trust_root)
    expected = index["phases"][args.phase]
    require(current.sequence + 1 == expected["sequence"], "phase_must_advance_exactly_once")
    require(any(sha(current_bytes) == phase["sha256"] for phase in index["phases"].values()), "unknown_active_catalog")
    temporary = target.with_name("catalog.next.private.json")
    require(not temporary.exists(), "unfinished_local_activation")
    data = (args.bundle / "phases" / f"{args.phase}.signed.json").read_bytes()
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return {"result": "local-phase-staged", "phase": args.phase, "sequence": expected["sequence"], "published": False}


def local_preferences(config):
    local = [
        {key: value for key, value in entry.items() if key != "manifest"}
        for entry in config["models"]
        if entry.get("execution") == "local"
    ]
    return sha(
        encode(
            {
                "models": local,
                "contribution_policy": config["contribution_policy"],
                "inference_mode": config["inference_mode"],
                "workers": config.get("workers", []),
            }
        )
    )


def observe(args, *, get_status=None, monotonic=time.monotonic, sleep=time.sleep):
    index, bootstrap = load_bundle(args.bundle)
    require(1 <= args.timeout <= MAX_SECONDS, "observation_timeout_bounds")
    parsed = urlsplit(args.node_url)
    require(
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "::1", "localhost")
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment,
        "node_url_must_be_loopback",
    )
    previous = None
    if args.phase != "baseline":
        require(args.previous is not None, "previous_phase_evidence_required")
        previous = json.loads(args.previous.read_text())
        require(
            previous["result"] == "passed" and previous["trust_root_digest"] == bootstrap.trust_root_digest,
            "previous_evidence_mismatch",
        )
        require(PHASES.index(args.phase) == PHASES.index(previous["phase"]) + 1, "observation_phase_order")
    args.output.mkdir(parents=True, exist_ok=False)
    result = {
        "schema_version": 1,
        "result": "failed",
        "scope": "ordinary-canary-catalog-refresh-observation",
        "complete_gate16": False,
        "phase": args.phase,
        "trust_root_digest": bootstrap.trust_root_digest,
        "driver_sha256": sha(Path(__file__).read_bytes()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    began = monotonic()
    client = None
    try:
        if get_status is None:
            sys.path.insert(0, str(ROOT / "desktop/src"))
            from communityai_desktop.credentials import NativeCredentialStore

            secret = NativeCredentialStore(args.credential_service, args.credential_account).get()
            client = httpx.Client(
                base_url=args.node_url,
                timeout=5,
                trust_env=False,
                follow_redirects=False,
                headers={"Authorization": "Bearer " + secret},
            )

            def get_status():
                response = client.get("/control/v1/status")
                response.raise_for_status()
                return response.json()

        while True:
            require(monotonic() - began <= args.timeout, "catalog_observation_deadline")
            config_bytes = args.node_config.read_bytes()
            config = NodeConfig.from_json(config_bytes.decode("utf-8"), base_dir=args.node_config.resolve().parent)
            installed_root = CatalogBootstrapConfig.load(config.catalog_bootstrap_path)
            require(installed_root.trust_root_digest == bootstrap.trust_root_digest, "consumer_not_on_canary_root")
            installed = SignedModelCatalog.from_json(config.catalog_path.read_text()).verify(bootstrap.trust_root)
            try:
                status = get_status()
            except (httpx.TransportError, httpx.TimeoutException):
                sleep(0.5)
                continue
            if status.get("status") != "running":
                sleep(0.5)
                continue
            started_at = status.get("started_at")
            require(type(started_at) is int and started_at > 0, "node_start_identity_unavailable")
            expected = index["phases"][args.phase]
            if installed.digest != expected["catalog_digest"] or (
                previous and started_at <= previous["node_started_at"]
            ):
                sleep(0.5)
                continue
            require(installed.sequence == expected["sequence"], "installed_sequence_mismatch")
            config_revision = "sha256:" + sha(config_bytes)
            if (
                args.node_config.read_bytes() != config_bytes
                or status["contribution"]["policy"].get("config_revision") != config_revision
            ):
                sleep(0.5)
                continue
            require(
                status["inference_mode"] == "local_only"
                and not status["contribution"]["policy"]["policy"]["sharing_enabled"],
                "consumer_must_remain_non_contributing_local_only",
            )
            require(all(worker["state"] == "paused" for worker in status["workers"]), "unexpected_running_worker")
            require(status["runtime_budget"]["resident_models"] == 0, "unexpected_model_load")
            document = json.loads(config_bytes)
            preferences = local_preferences(document)
            if previous:
                require(preferences == previous["local_preferences_sha256"], "consumer_preferences_changed")
            if args.phase == "withdrawal":
                require(
                    index["community_manifest_digest"] not in config.auto_model_priority,
                    "withdrawn_model_still_automatic",
                )
                require(
                    all(model.execution == "local" for model in installed.models), "withdrawal_still_approves_community"
                )
            else:
                require(
                    index["community_manifest_digest"] in config.auto_model_priority, "community_priority_not_restored"
                )
            result.update(
                result="passed",
                node_started_at=started_at,
                catalog_digest=installed.digest,
                catalog_sequence=installed.sequence,
                config_revision=config_revision,
                local_preferences_sha256=preferences,
                node_restarted=previous is not None,
                no_model_load=True,
                sharing_paused=True,
            )
            break
    except BaseException as exc:
        result["error_type"] = type(exc).__name__
        if isinstance(exc, ChannelError):
            result["error_code"] = str(exc)
    finally:
        closed = client is None
        try:
            if client is not None:
                client.close()
                closed = True
        except BaseException as exc:
            result["result"] = "failed"
            result["cleanup_error_type"] = type(exc).__name__
        result["duration_seconds"] = round(monotonic() - began, 3)
        result["cleanup"] = {"created_processes": 0, "created_credentials": 0, "owned_http_client_closed": closed}
        result["limitations"] = [
            "Prepared private state and an existing HTTPS channel are required; publishing is outside this driver.",
            "No active-generation drain, inference, worker health reconstruction, GUI visibility, or full canary cleanup is established.",
            "The observer never stops the external desktop or deletes its native credential; its owning lifecycle must do so.",
            "Authenticated active config revision and a later running node bind saved policy; the API exposes no active catalog digest.",
        ]
        (args.output / "result.json").write_bytes(encode(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    stage = modes.add_parser("prepare")
    stage.add_argument("--release", type=Path, required=True)
    stage.add_argument("--base-url", required=True)
    stage.add_argument("--initial-peer", required=True)
    stage.add_argument("--run-id", required=True)
    stage.add_argument("--output", type=Path, required=True)
    advance_parser = modes.add_parser("advance-local")
    advance_parser.add_argument("--bundle", type=Path, required=True)
    advance_parser.add_argument("--phase", choices=("withdrawal", "restore"), required=True)
    watch = modes.add_parser("observe")
    watch.add_argument("--bundle", type=Path, required=True)
    watch.add_argument("--phase", choices=PHASES, required=True)
    watch.add_argument("--previous", type=Path)
    watch.add_argument("--node-config", type=Path, required=True)
    watch.add_argument("--node-url", default="http://127.0.0.1:18116")
    watch.add_argument("--credential-service", required=True)
    watch.add_argument("--credential-account", default="control")
    watch.add_argument("--timeout", type=int, default=420)
    watch.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = (
        prepare(args) if args.mode == "prepare" else advance(args) if args.mode == "advance-local" else observe(args)
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key in ("result", "phase", "complete_gate16", "published")}
        )
    )
    if result["result"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
