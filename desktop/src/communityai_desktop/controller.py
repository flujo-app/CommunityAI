"""Shell-neutral desktop actions and presentation snapshots."""

from __future__ import annotations

from typing import Any, Dict

from communityai_desktop.client import NodeClient


class DesktopController:
    def __init__(self, client: NodeClient):
        self.client = client

    def snapshot(self) -> Dict[str, Any]:
        status = self.client.status()
        models = [self._model_view(model) for model in status["models"]]
        workers = [self._worker_view(worker) for worker in status["workers"]]
        return {
            "node_status": status.get("status", "unknown"),
            "openai_base_url": status["openai_base_url"],
            "started_at": status.get("started_at"),
            "runtime_budget": status.get("runtime_budget", {}),
            "models": models,
            "workers": workers,
            "keys": [self._key_view(key) for key in self.client.list_keys()],
            "network": self._network_view(status.get("network"), models),
            "contribution": self._contribution_view(status.get("contribution"), workers),
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
            "covered_blocks": covered,
            "total_blocks": total,
            "peer_count": route.get("peer_count"),
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

    @staticmethod
    def _key_view(key: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(key.get("id", "unknown")),
            "label": str(key.get("label", "unknown")),
            "fingerprint": str(key.get("fingerprint", "unknown")),
            "created_at": key.get("created_at"),
            "revoked_at": key.get("revoked_at"),
        }

    @staticmethod
    def _network_view(network: Any, models: list[Dict[str, Any]]) -> Dict[str, Any]:
        network = network if isinstance(network, dict) else {}
        regions = network.get("regions") if isinstance(network.get("regions"), list) else []
        clean_regions = []
        for region in regions:
            if not isinstance(region, dict):
                continue
            name, count = region.get("name"), region.get("peers")
            if isinstance(name, str) and isinstance(count, int) and count >= 0:
                clean_regions.append({"name": name, "peers": count})
        inferred_counts = [model["peer_count"] for model in models if isinstance(model.get("peer_count"), int)]
        peer_count = network.get("peer_count")
        if not isinstance(peer_count, int) or peer_count < 0:
            peer_count = max(inferred_counts, default=0)
        return {"peer_count": peer_count, "regions": clean_regions}

    @staticmethod
    def _contribution_view(contribution: Any, workers: list[Dict[str, Any]]) -> Dict[str, Any]:
        contribution = contribution if isinstance(contribution, dict) else {}
        percent = contribution.get("gpu_memory_percent", 50)
        if not isinstance(percent, int) or not 10 <= percent <= 100:
            percent = 50
        total_bytes = contribution.get("gpu_memory_total_bytes")
        if not isinstance(total_bytes, int) or total_bytes <= 0:
            total_bytes = None
        active_models = sorted({worker["model"] for worker in workers if worker["desired_running"]})
        return {
            "enabled": bool(active_models),
            "gpu_name": str(contribution.get("gpu_name", "Your GPU")),
            "gpu_memory_percent": percent,
            "gpu_memory_total_bytes": total_bytes,
            "active_models": active_models,
        }

    def worker_action(self, worker_id: str, action: str) -> Dict[str, Any]:
        return self.client.worker_action(worker_id, action)

    def set_workers_enabled(self, worker_ids: list[str], enabled: bool) -> list[Dict[str, Any]]:
        action = "start" if enabled else "pause"
        return [self.client.worker_action(worker_id, action) for worker_id in worker_ids]

    def create_client_key(self, label: str) -> Dict[str, Any]:
        return self.client.create_key(label)

    def relabel_client_key(self, key_id: str, label: str) -> Dict[str, Any]:
        return self.client.relabel_key(key_id, label)

    def revoke_client_key(self, key_id: str) -> Dict[str, Any]:
        return self.client.revoke_key(key_id)
