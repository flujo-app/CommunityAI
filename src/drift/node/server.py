"""HTTP composition for the persistent local node."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import List

from fastapi import HTTPException, Request
from pydantic import BaseModel

from drift.api.server import create_app
from drift.node.model_manager import (
    ModelInUseError,
    ModelManager,
    ModelManagerClosedError,
    ModelNotFoundError,
    ModelUnloadError,
)

CONTROL_API_VERSION = 1


class ModelUnloadRequest(BaseModel):
    model: str


def create_node_app(
    model_manager: ModelManager,
    *,
    api_keys: List[str],
    host: str = "127.0.0.1",
    port: int = 8080,
    max_concurrent: int = 1,
    default_max_tokens: int = 512,
):
    """Compose the OpenAI API and authenticated local control surface."""
    if not api_keys or any(not isinstance(key, str) or not key for key in api_keys):
        raise ValueError("the node control API requires at least one non-empty API key")
    app = create_app(
        model_manager=model_manager,
        api_keys=api_keys,
        max_concurrent=max_concurrent,
        default_max_tokens=default_max_tokens,
    )
    started_at = int(time.time())

    def check_auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        candidate = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        if not any(secrets.compare_digest(candidate, key) for key in api_keys):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/control/v1/status")
    async def node_status(request: Request):
        check_auth(request)
        return {
            "api_version": CONTROL_API_VERSION,
            "status": "stopping" if model_manager.closed else "running",
            "started_at": started_at,
            "openai_base_url": f"http://{'[' + host + ']' if ':' in host else host}:{port}/v1",
            "runtime_budget": model_manager.residency(),
            "models": [snapshot.to_dict() for snapshot in model_manager.snapshots()],
        }

    @app.post("/control/v1/models/unload")
    async def unload_model(body: ModelUnloadRequest, request: Request):
        check_auth(request)
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

    app.router.add_event_handler("shutdown", model_manager.shutdown)
    app.state.model_manager = model_manager
    return app
