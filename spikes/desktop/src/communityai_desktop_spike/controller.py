"""Shell-neutral desktop actions and presentation snapshots."""

from __future__ import annotations

from typing import Any, Dict

from communityai_desktop_spike.client import NodeClient


class DesktopController:
    def __init__(self, client: NodeClient):
        self.client = client

    def snapshot(self) -> Dict[str, Any]:
        status = self.client.status()
        return {
            "node_status": status.get("status", "unknown"),
            "openai_base_url": status["openai_base_url"],
            "started_at": status.get("started_at"),
            "runtime_budget": status.get("runtime_budget", {}),
            "models": [self._model_view(model) for model in status["models"]],
            "workers": [self._worker_view(worker) for worker in status["workers"]],
        }

    @staticmethod
    def _model_view(model: Dict[str, Any]) -> Dict[str, Any]:
        route = model.get("route") if isinstance(model.get("route"), dict) else {}
        covered = route.get("covered_blocks")
        total = route.get("total_blocks")
        coverage = f"{covered}/{total}" if isinstance(covered, int) and isinstance(total, int) else "unknown"
        return {
            "id": str(model.get("id", "unknown")),
            "state": str(model.get("state", "unknown")),
            "coverage": coverage,
            "active_requests": model.get("active_requests", 0),
            "last_error": model.get("last_error"),
        }

    @staticmethod
    def _worker_view(worker: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(worker.get("id", "unknown")),
            "model": str(worker.get("model", "unknown")),
            "state": str(worker.get("state", "unknown")),
            "desired_running": bool(worker.get("desired_running", False)),
            "restart_count": worker.get("restart_count", 0),
            "last_error": worker.get("last_error"),
        }

    def worker_action(self, worker_id: str, action: str) -> Dict[str, Any]:
        return self.client.worker_action(worker_id, action)

    def create_client_key(self, label: str) -> Dict[str, Any]:
        return self.client.create_key(label)
