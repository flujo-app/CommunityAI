"""Bound optional display telemetry without changing routing or download decisions."""

import math
import time


def number(value, *, integer=False, maximum=64 * 1024**4):
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        return None
    return value if math.isfinite(value) and 0 <= value <= maximum else None


def text(value, limit=256):
    return " ".join(value.split())[:limit] if isinstance(value, str) else None


def download_view(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    state = value.get("state")
    if state not in {
        "waiting",
        "checking",
        "downloading",
        "retrying",
        "verifying",
        "loading",
        "ready",
        "failed",
        "paused",
    }:
        return None
    result = {"state": state, "artifact": text(value.get("artifact"))}
    for key in (
        "artifact_bytes",
        "artifact_received_bytes",
        "selected_bytes",
        "received_bytes",
        "verified_bytes",
        "verified_files",
        "selected_files",
        "resumed_bytes",
        "retries",
    ):
        result[key] = number(value.get(key), integer=True)
    result["bytes_per_second"] = number(value.get("bytes_per_second"))
    result["updated_at"] = number(value.get("updated_at"))
    if result["updated_at"] is not None and time.time() - result["updated_at"] > 10:
        result["bytes_per_second"] = 0
    return result


def route_view(route):
    total = number(route.get("total_blocks"), integer=True, maximum=4096)
    total = total or 0

    def counts(key):
        values = route.get(key)
        if not isinstance(values, list) or len(values) != total:
            return None
        return [number(value, integer=True, maximum=100000) for value in values]

    def blocks(value):
        if not isinstance(value, list) or len(value) > total:
            return []
        return sorted({index for index in value if type(index) is int and 0 <= index < total})

    def records(key):
        value = route.get(key)
        return value[:256] if isinstance(value, list) else []

    peers = []
    for peer in records("peers"):
        if not isinstance(peer, dict) or not isinstance(peer.get("peer_id"), str):
            continue
        peers.append(
            {
                "peer_id": text(peer["peer_id"], 128),
                "public_name": text(peer.get("public_name"), 128),
                "online_blocks": blocks(peer.get("online_blocks")),
                "joining_blocks": blocks(peer.get("joining_blocks")),
                "offline_blocks": blocks(peer.get("offline_blocks")),
                "version": text(peer.get("version"), 64),
                "torch_dtype": text(peer.get("torch_dtype"), 32),
                "quant_type": text(peer.get("quant_type"), 32),
                "using_relay": peer.get("using_relay") if type(peer.get("using_relay")) is bool else None,
            }
        )
    reservations = []
    for reservation in records("reservations"):
        if not isinstance(reservation, dict):
            continue
        start = number(reservation.get("start_block"), integer=True, maximum=total)
        end = number(reservation.get("end_block"), integer=True, maximum=total)
        expiry = number(reservation.get("expires_at"))
        if start is None or end is None or start >= end or expiry is None or expiry <= time.time():
            continue
        reservations.append(
            {
                "peer_id": text(reservation.get("peer_id"), 128),
                "start_block": start,
                "end_block": end,
                "expires_at": expiry,
            }
        )
    return {
        "total_blocks": total,
        "replica_counts": counts("replica_counts"),
        "joining_counts": counts("joining_counts"),
        "offline_counts": counts("offline_counts"),
        "peers": peers,
        "reservations": reservations,
        "reservations_known": isinstance(route.get("reservations"), list),
        "status": text(route.get("status"), 32),
        "last_updated_age": number(route.get("last_updated_age")),
        "last_error": text(route.get("last_error")),
        "truncated": route.get("peer_details_truncated") is True,
    }
