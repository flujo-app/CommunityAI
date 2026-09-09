"""Shell-neutral desktop actions and presentation snapshots."""

from __future__ import annotations

from typing import Any, Dict

from communityai_desktop.client import NodeApiError, NodeClient, NodeClientError
from communityai_desktop.telemetry import route_view


def _download_storage_estimate(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Pending verified shard selection"
    return f"{size_bytes / 1_000_000_000:.1f} GB ({size_bytes:,} bytes)"


class DesktopController:
    def __init__(self, client: NodeClient):
        self.client = client

    def snapshot(self) -> Dict[str, Any]:
        status = self.client.status()
        auto_selection = self._auto_selection_view(status.get("auto_selection"))
        models = [self._model_view(model) for model in status["models"]]
        for model in models:
            model["auto_selected"] = model["id"] == auto_selection["model"]
        contribution = status["contribution"]
        workers = [self._worker_view(worker) for worker in contribution["workers"]]
        hardware = dict(status.get("hardware") or {})
        selected = next((model for model in models if model["auto_selected"]), {})
        hardware["inference_device"] = selected.get("device")
        return {
            "node_status": status.get("status", "unknown"),
            "openai_base_url": status["openai_base_url"],
            "started_at": status.get("started_at"),
            "runtime_budget": status.get("runtime_budget", {}),
            "hardware": hardware,
            "inference_mode": status.get("inference_mode", "auto"),
            "inference_mode_editable": status.get("inference_mode_editable", False),
            "models": models,
            "auto_selection": auto_selection,
            "workers": workers,
            "keys": [self._key_view(key) for key in self.client.list_keys()],
            "network": self._network_view(status.get("network"), models),
            "contribution": self._contribution_view(contribution, workers, hardware),
        }

    @staticmethod
    def _model_view(model: Dict[str, Any]) -> Dict[str, Any]:
        route = model.get("route") if isinstance(model.get("route"), dict) else {}
        covered = route.get("covered_blocks")
        total = route.get("total_blocks")
        coverage = f"{covered}/{total}" if isinstance(covered, int) and isinstance(total, int) else "unknown"
        selected_whole_shard_bytes = model["download"]["selected_whole_shard_bytes"]
        route_complete = (
            route.get("status") == "complete"
            and isinstance(covered, int)
            and isinstance(total, int)
            and total > 0
            and covered == total
        )
        return {
            "id": str(model.get("id", "unknown")),
            "state": str(model.get("state", "unknown")),
            "coverage": coverage,
            "covered_blocks": covered,
            "total_blocks": total,
            "route_complete": route_complete,
            "peer_count": route.get("peer_count"),
            "execution": "local" if route.get("source") == "local" else "distributed",
            "device": route.get("device"),
            "selected_whole_shard_bytes": selected_whole_shard_bytes,
            "download_storage_estimate": _download_storage_estimate(selected_whole_shard_bytes),
            "active_requests": model.get("active_requests", 0),
            "last_error": model.get("last_error"),
            "health": route_view(route),
            "download_progress": model["download"].get("progress"),
        }

    @staticmethod
    def _auto_selection_view(selection: Any) -> Dict[str, Any]:
        selection = selection if isinstance(selection, dict) else {}
        status = str(selection.get("status", "not_configured"))
        model = selection.get("model") if isinstance(selection.get("model"), str) else None
        reason = str(selection.get("reason", "Automatic model selection is not configured."))
        if status == "selected" and model is not None:
            title = f"auto selects {model}"
        elif status == "unavailable":
            title = "auto is waiting for a complete route"
        else:
            title = "auto is not configured"
        return {
            "status": status,
            "model": model,
            "manifest_digest": selection.get("manifest_digest"),
            "reason": reason,
            "covered_blocks": selection.get("covered_blocks"),
            "total_blocks": selection.get("total_blocks"),
            "peer_count": selection.get("peer_count"),
            "source": selection.get("source"),
            "title": title,
        }

    @staticmethod
    def _worker_view(worker: Dict[str, Any]) -> Dict[str, Any]:
        policy = worker["policy"]
        schedule = worker["schedule"]
        resources = worker["resources"]
        admitted = policy["admitted"] and schedule["admitted"] and resources["admitted"]
        blocked_reason = next(
            (gate["reason"] for gate in (policy, schedule, resources) if not gate["admitted"]),
            None,
        )
        state = worker["state"]
        desired_running = worker["desired_running"]
        progress_state = (worker.get("download_progress") or {}).get("state")
        preparing = state in ("running", "starting") and progress_state in {
            "waiting",
            "checking",
            "downloading",
            "retrying",
            "verifying",
            "loading",
        }
        if preparing:
            display_status = "Downloading model" if progress_state in ("downloading", "retrying") else "Preparing model"
        elif state == "running":
            display_status = "Sharing"
        elif state == "starting":
            display_status = "Starting sharing"
        elif desired_running and blocked_reason:
            display_status = f"Waiting: {blocked_reason}"
        elif not admitted:
            display_status = f"Blocked: {blocked_reason}"
        elif state == "crashed":
            display_status = "Stopped unexpectedly"
        else:
            display_status = "Not sharing"
        return {
            "id": worker["id"],
            "model": worker["model"],
            "state": state,
            "desired_running": desired_running,
            "operator_paused": worker.get("operator_paused", False),
            "sharing_active": state == "running" and not preparing,
            "preparing": preparing,
            "can_start": admitted,
            "blocked_reason": blocked_reason,
            "display_status": display_status,
            "preferred": policy["preferred"],
            "policy_admitted": policy["admitted"],
            "policy_reason": policy["reason"],
            "schedule_admitted": schedule["admitted"],
            "schedule_reason": schedule["reason"],
            "schedule_suspended": schedule["suspended"],
            "resource_admitted": resources["admitted"],
            "resource_reason": resources["reason"],
            "resource_suspended": resources["suspended"],
            "limits": resources["limits"],
            "measurements": resources["measurements"],
            "placement": worker.get("placement", {}),
            "download_progress": worker.get("download_progress"),
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
    def _contribution_view(
        contribution: Dict[str, Any], workers: list[Dict[str, Any]], hardware: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        policy_snapshot = contribution["policy"]
        policy = policy_snapshot["policy"]
        active_models = sorted({worker["model"] for worker in workers if worker["sharing_active"]})
        selected_models = sorted({worker["model"] for worker in workers if worker["desired_running"]})
        blocked_reasons = []
        selected_blocked_reasons = []
        for worker in workers:
            reason = worker["blocked_reason"]
            if reason and reason not in blocked_reasons:
                blocked_reasons.append(reason)
            selected = worker["desired_running"] or (
                policy.get("sharing_enabled")
                and worker.get("placement", {}).get("automatic")
                and not worker.get("operator_paused", False)
            )
            if selected and reason and reason not in selected_blocked_reasons:
                selected_blocked_reasons.append(reason)

        vram_pairs = {
            (worker["limits"]["vram_bytes"], worker["limits"]["vram_pool_bytes"])
            for worker in workers
            if worker["limits"]["vram_bytes"] is not None
        }
        if len(vram_pairs) == 1 and all(worker["limits"]["vram_bytes"] is not None for worker in workers):
            vram_bytes, vram_pool_bytes = next(iter(vram_pairs))
            vram_percent = round(vram_bytes * 100 / vram_pool_bytes)
            vram_status = "configured"
        elif vram_pairs:
            vram_bytes = vram_pool_bytes = vram_percent = None
            vram_status = "varies"
        else:
            vram_bytes = vram_pool_bytes = vram_percent = None
            vram_status = "unavailable"

        hardware = hardware or {}
        if hardware.get("sharing_vram_bytes") is not None:
            vram_bytes = hardware["sharing_vram_bytes"]
            vram_pool_bytes = hardware.get("gpu_total_bytes")
            vram_percent = round(vram_bytes * 100 / vram_pool_bytes) if vram_pool_bytes else None
            vram_status = "configured"
        intent_enabled = policy.get("sharing_enabled", False) or bool(selected_models)
        return {
            "configured": contribution["configured"],
            "editable": contribution["editable"],
            "config_revision": policy_snapshot["config_revision"],
            "policy": policy,
            "enabled": bool(active_models),
            "intent_enabled": intent_enabled,
            "can_start": contribution["editable"] and bool(workers),
            "can_pause": intent_enabled,
            "active_models": active_models,
            "selected_models": selected_models,
            "blocked_reasons": blocked_reasons,
            "selected_blocked_reasons": selected_blocked_reasons,
            "vram_status": vram_status,
            "vram_bytes": vram_bytes,
            "vram_pool_bytes": vram_pool_bytes,
            "vram_percent": vram_percent,
            "processing_percent": policy.get("max_processing_percent", 100),
            "vram_available_bytes": hardware.get("sharing_vram_available_bytes"),
        }

    def set_sharing_enabled(self, enabled: bool) -> Dict[str, Any]:
        """Persist the user's sharing choice before starting or stopping workers."""
        current = self.client.status()["contribution"]
        if not current["editable"]:
            raise NodeClientError("Sharing settings are unavailable. Restart CommunityAI and try again.")
        if enabled and not current["workers"]:
            raise NodeClientError("No community model is available for sharing yet.")
        saved = current["policy"]
        for worker in current["workers"]:
            self.client.worker_action(worker["id"], "pause")
        policy = {**saved["policy"], "sharing_enabled": enabled}
        if enabled:
            policy["max_disk_space"] = policy.get("max_disk_space") or "20GiB"
            policy["max_vram"] = policy.get("max_vram") or "100%"
            policy["max_processing_percent"] = policy.get("max_processing_percent", 100)
        result = self.client.update_contribution_policy(policy, expected_revision=saved["config_revision"])
        if not enabled:
            return {**result, "message": "Sharing paused."}
        waiting = False
        for worker in current["workers"]:
            try:
                self.client.worker_action(worker["id"], "start")
            except NodeApiError as exc:
                if exc.status_code != 409:
                    raise
                waiting = True
        return {**result, "message": "Sharing is waiting to start." if waiting else "Sharing enabled."}

    def worker_action(self, worker_id: str, action: str) -> Dict[str, Any]:
        return self.client.worker_action(worker_id, action)

    def update_contribution_policy(self, policy: Dict[str, Any], *, expected_revision: str) -> Dict[str, Any]:
        return self.client.update_contribution_policy(policy, expected_revision=expected_revision)

    def update_resource_limits(self, changes: Dict[str, Any], *, expected_revision: str) -> Dict[str, Any]:
        if not changes or set(changes) - {"max_vram", "max_processing_percent"}:
            raise ValueError("Only VRAM and processing percentages can change here")
        for field, value in changes.items():
            if field == "max_vram":
                if not isinstance(value, str) or not value.endswith("%") or not value[:-1].isdigit():
                    raise ValueError("VRAM must be a whole percentage")
                value = int(value[:-1])
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError("Resource percentages must be whole numbers from 1 to 100")
        current = self.client.status()["contribution"]
        saved = current["policy"]
        if saved["config_revision"] != expected_revision:
            raise NodeClientError("Settings changed elsewhere. Refresh before applying limits.")
        if not current["editable"] or "max_processing_percent" not in saved["policy"]:
            raise NodeClientError("Update the local node to use these resource controls.")
        resume = [
            worker["id"]
            for worker in current["workers"]
            if worker["desired_running"]
            or (
                saved["policy"].get("sharing_enabled")
                and worker.get("placement", {}).get("automatic")
                and not worker.get("operator_paused", False)
            )
        ]
        # Pause all configured workers, including automatic workers with no current
        # process. This prevents the placement service racing the policy transaction.
        for worker in current["workers"]:
            self.client.worker_action(worker["id"], "pause")
        result = self.client.update_contribution_policy(
            {**saved["policy"], **changes}, expected_revision=expected_revision
        )
        # Failed persistence deliberately leaves workers stopped. Never restart a
        # worker with an old, more permissive limit after a rejected save.
        errors = []
        for worker_id in resume:
            try:
                self.client.worker_action(worker_id, "start")
            except NodeClientError as exc:
                errors.append(str(exc))
        result["message"] = "Changes saved. Sharing is waiting to start." if errors else "Changes saved."
        return result

    def set_workers_enabled(self, worker_ids: list[str], enabled: bool) -> list[Dict[str, Any]]:
        action = "start" if enabled else "pause"
        return [self.client.worker_action(worker_id, action) for worker_id in worker_ids]

    def create_client_key(self, label: str) -> Dict[str, Any]:
        return self.client.create_key(label)

    def relabel_client_key(self, key_id: str, label: str) -> Dict[str, Any]:
        return self.client.relabel_key(key_id, label)

    def revoke_client_key(self, key_id: str) -> Dict[str, Any]:
        return self.client.revoke_key(key_id)
