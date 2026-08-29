"""HTTP composition for the persistent local node."""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from typing import Callable, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from drift.api.server import create_app
from drift.node.config import ContributionPolicyConfig, NodeConfigError
from drift.node.keys import ApiKeyNotFoundError, ApiKeyStore, ApiKeyStoreError, LastActiveKeyError
from drift.node.model_manager import (
    ModelInUseError,
    ModelManager,
    ModelManagerClosedError,
    ModelNotFoundError,
    ModelUnloadError,
)
from drift.node.policy_store import (
    ContributionPolicyConflictError,
    ContributionPolicyPersistenceError,
    ContributionPolicyStore,
    parse_policy_update_request,
)
from drift.node.worker_supervisor import (
    WorkerNotFoundError,
    WorkerPolicyError,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
)

CONTROL_API_VERSION = 1
CONTRIBUTION_STATUS_SCHEMA_VERSION = 3


def _bounded_text(value, fallback: str, *, limit: int = 300) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.split())
    if not normalized or not normalized.isprintable():
        return fallback
    return normalized[:limit]


def _optional_positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _optional_nonnegative_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def _optional_positive_number(value):
    value = _optional_nonnegative_number(value)
    return value if value is not None and value > 0 else None


def _gate_status(snapshot, prefix: str):
    admitted = snapshot.get(f"{prefix}_admitted") is True
    reason = snapshot.get(f"{prefix}_reason")
    return {
        "admitted": admitted,
        "reason": (None if admitted else _bounded_text(reason, f"{prefix.capitalize()} status is unavailable")),
        **({"suspended": snapshot.get(f"{prefix}_suspended") is True} if prefix in ("schedule", "resource") else {}),
    }


def _contribution_status(worker_snapshots, *, configured: bool, editable: bool, policy_snapshot):
    """Return the bounded, secret-free worker view consumed by the desktop."""
    workers = []
    for snapshot in worker_snapshots:
        workers.append(
            {
                "id": _bounded_text(snapshot.get("id"), "unknown worker", limit=128),
                "model": _bounded_text(snapshot.get("model"), "unknown model", limit=256),
                "state": (
                    snapshot.get("state")
                    if snapshot.get("state") in ("paused", "starting", "running", "stopping", "crashed")
                    else "unknown"
                ),
                "desired_running": snapshot.get("desired_running") is True,
                "placement": {
                    "automatic": snapshot.get("automatic") is True,
                    "block_indices": (
                        _bounded_text(snapshot.get("block_indices"), "unassigned", limit=64)
                        if snapshot.get("automatic") is True
                        else None
                    ),
                    "reason": (
                        _bounded_text(snapshot.get("placement_reason"), "placement is pending")
                        if snapshot.get("automatic") is True
                        else None
                    ),
                },
                "policy": {
                    **_gate_status(snapshot, "policy"),
                    "preferred": snapshot.get("preferred") is True,
                },
                "schedule": _gate_status(snapshot, "schedule"),
                "resources": {
                    **_gate_status(snapshot, "resource"),
                    "limits": {
                        "disk_bytes": _optional_positive_int(snapshot.get("max_disk_bytes")),
                        "vram_bytes": _optional_positive_int(snapshot.get("max_vram_bytes")),
                        "vram_pool_bytes": _optional_positive_int(snapshot.get("vram_pool_bytes")),
                        "bandwidth_mbps": _optional_positive_number(snapshot.get("max_bandwidth_mbps")),
                        "power_watts": _optional_positive_number(snapshot.get("max_power_watts")),
                    },
                    "measurements": {
                        "bandwidth_mbps": _optional_nonnegative_number(snapshot.get("current_bandwidth_mbps")),
                        "power_watts": _optional_nonnegative_number(snapshot.get("current_power_watts")),
                    },
                },
            }
        )
    return {
        "schema_version": CONTRIBUTION_STATUS_SCHEMA_VERSION,
        "configured": configured,
        "editable": editable,
        "policy": policy_snapshot,
        "workers": workers,
    }


class ModelUnloadRequest(BaseModel):
    model: str


class ApiKeyCreateRequest(BaseModel):
    label: str


class ApiKeyUpdateRequest(BaseModel):
    label: str


def create_node_app(
    model_manager: ModelManager,
    *,
    api_keys: Optional[List[str]] = None,
    api_key_store: Optional[ApiKeyStore] = None,
    control_keys: Optional[List[str]] = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    max_concurrent: int = 1,
    default_max_tokens: int = 512,
    worker_supervisor: Optional[WorkerSupervisor] = None,
    contribution_policy: Optional[ContributionPolicyConfig] = None,
    contribution_policy_store: Optional[ContributionPolicyStore] = None,
    route_outcome_observer: Optional[Callable[..., None]] = None,
):
    """Compose the OpenAI API and authenticated local control surface."""
    if api_key_store is None and (not api_keys or any(not isinstance(key, str) or not key for key in api_keys)):
        raise ValueError("the node OpenAI API requires an API key store or at least one non-empty API key")
    if api_key_store is not None and api_keys:
        raise ValueError("pass either api_key_store or api_keys, not both")
    if not control_keys or any(not isinstance(key, str) or not key for key in control_keys):
        raise ValueError("the node control API requires at least one non-empty control key")
    if len(set(control_keys)) != len(control_keys):
        raise ValueError("control keys must not contain duplicates")
    if api_key_store is not None:
        overlaps = any(api_key_store.contains(key) for key in control_keys)
    else:
        overlaps = any(
            secrets.compare_digest(control_key, api_key) for control_key in control_keys for api_key in api_keys
        )
    if overlaps:
        raise ValueError("control keys must be distinct from OpenAI API keys")
    if contribution_policy_store is not None and worker_supervisor is None:
        raise ValueError("persistent contribution policy requires a worker supervisor")
    control_keys = tuple(control_keys)
    app = create_app(
        model_manager=model_manager,
        api_keys=api_keys,
        api_key_verifier=api_key_store.verify if api_key_store is not None else None,
        max_concurrent=max_concurrent,
        default_max_tokens=default_max_tokens,
        route_outcome_observer=route_outcome_observer,
    )
    started_at = int(time.time())

    def check_control_auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        candidate = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        valid = any(secrets.compare_digest(candidate, key) for key in control_keys)
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid control key")

    @app.get("/control/v1/status")
    async def node_status(request: Request):
        check_control_auth(request)
        worker_snapshots = list(worker_supervisor.snapshots()) if worker_supervisor is not None else []
        if contribution_policy_store is not None:
            policy_snapshot = contribution_policy_store.snapshot()
        else:
            policy_snapshot = {
                "schema_version": 1,
                "config_revision": None,
                "policy": (
                    ContributionPolicyConfig() if contribution_policy is None else contribution_policy
                ).to_dict(),
            }
        contribution = _contribution_status(
            worker_snapshots,
            configured=worker_supervisor is not None,
            editable=contribution_policy_store is not None,
            policy_snapshot=policy_snapshot,
        )
        return {
            "api_version": CONTROL_API_VERSION,
            "status": "stopping" if model_manager.closed else "running",
            "started_at": started_at,
            "openai_base_url": f"http://{'[' + host + ']' if ':' in host else host}:{port}/v1",
            "runtime_budget": model_manager.residency(),
            "auto_selection": model_manager.auto_selection_snapshot(),
            "models": [snapshot.to_dict() for snapshot in model_manager.snapshots()],
            "workers": [
                {key: worker[key] for key in ("id", "model", "state", "desired_running")}
                for worker in contribution["workers"]
            ],
            "contribution": contribution,
        }

    def require_policy_store() -> ContributionPolicyStore:
        if contribution_policy_store is None:
            raise HTTPException(status_code=501, detail="persistent contribution policy editing is not configured")
        return contribution_policy_store

    @app.get("/control/v1/contribution-policy")
    async def get_contribution_policy(request: Request):
        check_control_auth(request)
        return require_policy_store().snapshot()

    @app.put("/control/v1/contribution-policy")
    async def update_contribution_policy(request: Request):
        check_control_auth(request)
        store = require_policy_store()
        if request.headers.get("content-type", "").split(";", 1)[0].strip().casefold() != "application/json":
            raise HTTPException(status_code=415, detail="contribution policy request must use application/json")
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 256 * 1024:
                raise HTTPException(status_code=413, detail="contribution policy request exceeds the size limit")
        try:
            expected_revision, policy = parse_policy_update_request(bytes(payload))
            return store.update(policy, expected_revision=expected_revision)
        except NodeConfigError as exc:
            raise HTTPException(status_code=422, detail=_bounded_text(str(exc), "invalid contribution policy")) from exc
        except ContributionPolicyConflictError as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except WorkerReconfigurationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ContributionPolicyPersistenceError as exc:
            raise HTTPException(status_code=503, detail="contribution policy persistence failed") from exc

    @app.post("/control/v1/models/unload")
    async def unload_model(body: ModelUnloadRequest, request: Request):
        check_control_auth(request)
        if not body.model.strip():
            raise HTTPException(status_code=422, detail="model must be a non-empty string")
        try:
            descriptor = model_manager.resolve(body.model)
            loop = asyncio.get_running_loop()
            unloaded = await loop.run_in_executor(None, model_manager.unload, body.model)
        except ModelNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ModelInUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelManagerClosedError as exc:
            raise HTTPException(status_code=503, detail="Node is shutting down") from exc
        except ModelUnloadError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"model": descriptor.model_id, "unloaded": unloaded}

    @app.get("/control/v1/workers")
    async def list_workers(request: Request):
        check_control_auth(request)
        return {"workers": list(worker_supervisor.snapshots()) if worker_supervisor is not None else []}

    def require_key_store() -> ApiKeyStore:
        if api_key_store is None:
            raise HTTPException(status_code=501, detail="persistent API-key management is not configured")
        return api_key_store

    @app.get("/control/v1/keys")
    async def list_api_keys(request: Request):
        check_control_auth(request)
        return {"keys": list(require_key_store().list())}

    @app.post("/control/v1/keys", status_code=201)
    async def create_api_key(body: ApiKeyCreateRequest, request: Request):
        check_control_auth(request)
        try:
            metadata, secret = require_key_store().create(label=body.label)
        except ApiKeyStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"key": metadata, "secret": secret}

    @app.delete("/control/v1/keys/{key_id}")
    async def revoke_api_key(key_id: str, request: Request):
        check_control_auth(request)
        try:
            metadata = require_key_store().revoke(key_id)
        except ApiKeyNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastActiveKeyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"key": metadata}

    @app.patch("/control/v1/keys/{key_id}")
    async def update_api_key(key_id: str, body: ApiKeyUpdateRequest, request: Request):
        check_control_auth(request)
        try:
            metadata = require_key_store().update_label(key_id, label=body.label)
        except ApiKeyNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApiKeyStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"key": metadata}

    async def worker_action(worker_id: str, action: str):
        if worker_supervisor is None:
            raise HTTPException(status_code=404, detail="no contribution workers are configured")
        try:
            loop = asyncio.get_running_loop()
            operation = {
                "start": worker_supervisor.start_worker,
                "pause": worker_supervisor.pause_worker,
                "restart": worker_supervisor.restart_worker,
            }[action]
            changed = await loop.run_in_executor(None, operation, worker_id)
            snapshot = worker_supervisor.snapshot(worker_id)
        except WorkerNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WorkerPolicyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"changed": changed, "worker": snapshot}

    @app.post("/control/v1/workers/{worker_id}/start")
    async def start_worker(worker_id: str, request: Request):
        check_control_auth(request)
        return await worker_action(worker_id, "start")

    @app.post("/control/v1/workers/{worker_id}/pause")
    async def pause_worker(worker_id: str, request: Request):
        check_control_auth(request)
        return await worker_action(worker_id, "pause")

    @app.post("/control/v1/workers/{worker_id}/restart")
    async def restart_worker(worker_id: str, request: Request):
        check_control_auth(request)
        return await worker_action(worker_id, "restart")

    if worker_supervisor is not None:
        app.router.add_event_handler("shutdown", worker_supervisor.shutdown)
    app.router.add_event_handler("shutdown", model_manager.shutdown)
    app.state.model_manager = model_manager
    return app
