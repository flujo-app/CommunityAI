"""Short, human-readable desktop summaries of observed node state."""

from __future__ import annotations

import re
from typing import Any


def model_name(value: str | None) -> str:
    if not value:
        return "No model selected"
    value = re.sub(r"[- ](?:Local|FP8[- ]Dequant)$", "", value, flags=re.IGNORECASE)
    return re.sub(r"(?<=\d)-(?=\d)|(?<=[A-Za-z])-?(?=\d+B\b)", " ", value)


def memory_text(value: int | float | None) -> str:
    if value is None:
        return "Not available"
    return f"{value / 1024**3:.1f} GB"


def model_summary(snapshot: dict[str, Any]) -> tuple[str, str, str]:
    selection = snapshot.get("auto_selection", {})
    selected = selection.get("model")
    if not selected:
        return "No model selected", "Open Models to check what is available.", ""
    model = next((item for item in snapshot.get("models", []) if item["id"] == selected), {})
    local = selection.get("source") == "local" or model.get("execution") == "local"
    if snapshot.get("inference_mode") == "local_only":
        reason = "You chose to use only this computer."
    elif local:
        reason = "The community model is not ready, so your messages use this computer."
    else:
        reason = "The community model is ready to answer your messages."
    return model_name(selected), reason, "On this computer" if local else "With the community"


def sharing_reason(reason: str | None) -> str:
    """Translate operational reasons without dumping internal policy text into the UI."""
    text = (reason or "").casefold()
    if any(word in text for word in ("vram", "gpu memory", "accelerator", "cuda", "memory budget")):
        if any(word in text for word in ("unavailable", "not available", "no cuda", "not detected")):
            return "Your graphics card is not available for sharing. Check its driver."
        return "Not enough GPU memory. Increase the memory limit or close another app."
    if any(word in text for word in ("disk", "storage", "artifact set")):
        return "Not enough storage. Free some space or increase the storage limit."
    if "schedule" in text:
        return "Sharing will start during the hours you chose."
    if any(word in text for word in ("power", "battery")):
        return "Sharing is paused to stay within your power settings."
    if "bandwidth" in text:
        return "Sharing is waiting for your download limit to allow it."
    if any(word in text for word in ("coverage", "discovery", "peer", "bootstrap", "connect", "network")):
        return "Connecting to the community. Sharing will start when connected."
    if any(word in text for word in ("placement", "candidate", "no eligible", "no community model")):
        return "Finding a model your computer can help with."
    if any(word in text for word in ("denied", "allowed", "disabled", "policy")):
        return "Your sharing settings are preventing this model from starting."
    if "download" in text:
        return "Downloading the files needed for sharing."
    if "changed elsewhere" in text:
        return "Your settings changed. Try again."
    return "Sharing could not start. Try again or check your settings."


def sharing_summary(snapshot: dict[str, Any]) -> tuple[str, str, str]:
    contribution = snapshot.get("contribution", {})
    workers = snapshot.get("workers", [])
    wanted = contribution.get("intent_enabled", False)
    active = [worker for worker in workers if worker.get("sharing_active", worker.get("state") == "running")]
    if active:
        names = ", ".join(
            dict.fromkeys(model_name(worker.get("model")) for worker in active if worker.get("model") != "auto")
        )
        return "Sharing is on", f"Helping with {names}." if names else "Helping the community.", "running"
    if not wanted:
        if contribution.get("editable") is False:
            return "Sharing is unavailable", "Restart CommunityAI to try again.", "waiting"
        return "Sharing is off", "", "off"
    selected = [
        worker
        for worker in workers
        if not worker.get("operator_paused")
        and (worker.get("desired_running") or (worker.get("placement") or {}).get("automatic"))
    ]
    if not selected:
        return "Sharing is paused", "", "paused"
    reasons = contribution.get("selected_blocked_reasons") or []
    if reasons:
        return "Sharing is waiting", sharing_reason(reasons[0]), "waiting"
    for worker in selected:
        progress = worker.get("download_progress") or {}
        if progress.get("state") in ("downloading", "verifying", "retrying"):
            return "Downloading for sharing", "You can keep using your computer.", "starting"
        if worker.get("state") == "crashed":
            return "Sharing stopped", "The model could not start. Try again.", "error"
        placement = worker.get("placement") or {}
        if placement.get("automatic") and not placement.get("block_indices"):
            reason = placement.get("reason")
            return (
                "Preparing to share",
                sharing_reason(reason) if reason else "Finding a part of the model for your computer.",
                "starting",
            )
    return "Starting sharing", "Preparing the model. You can keep using your computer.", "starting"
