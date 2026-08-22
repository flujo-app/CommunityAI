"""HTTP composition for the persistent local node."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from drift.api.server import create_app
from drift.node.keys import ApiKeyNotFoundError, ApiKeyStore, ApiKeyStoreError, LastActiveKeyError
from drift.node.model_manager import (
    ModelInUseError,
    ModelManager,
    ModelManagerClosedError,
    ModelNotFoundError,
    ModelUnloadError,
)
from drift.node.worker_supervisor import WorkerNotFoundError, WorkerSupervisor

CONTROL_API_VERSION = 1


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
    control_keys = tuple(control_keys)
    app = create_app(
        model_manager=model_manager,
        api_keys=api_keys,
        api_key_verifier=api_key_store.verify if api_key_store is not None else None,
        max_concurrent=max_concurrent,
        default_max_tokens=default_max_tokens,
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
        return {
            "api_version": CONTROL_API_VERSION,
            "status": "stopping" if model_manager.closed else "running",
            "started_at": started_at,
            "openai_base_url": f"http://{'[' + host + ']' if ':' in host else host}:{port}/v1",
            "runtime_budget": model_manager.residency(),
            "models": [snapshot.to_dict() for snapshot in model_manager.snapshots()],
            "workers": list(worker_supervisor.snapshots()) if worker_supervisor is not None else [],
        }

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
