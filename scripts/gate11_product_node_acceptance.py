"""Run the privacy-safe Gate 11 product-node inference/fallback probe."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA_VERSION = 1
PRIMARY_DIGEST = "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
STANDBY_DIGEST = "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd"


class AcceptanceError(RuntimeError):
    """A bounded acceptance condition was not met."""


def _read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceError("credential_file")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise AcceptanceError("credential_file")
    return secret


def _request_json(
    url: str,
    *,
    bearer: str,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
    timeout: float = 300,
) -> Mapping[str, Any]:
    payload = None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.load(response)
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
        raise AcceptanceError("http_request") from exc
    if not isinstance(document, dict):
        raise AcceptanceError("http_response")
    return document


def _route(status: Mapping[str, Any], digest: str) -> Mapping[str, Any]:
    for model in status.get("models", []):
        if isinstance(model, dict) and model.get("manifest_digest") == digest:
            route = model.get("route")
            if isinstance(route, dict):
                return route
    raise AcceptanceError("route_status")


def _route_complete(route: Mapping[str, Any]) -> bool:
    covered = route.get("covered_blocks")
    total = route.get("total_blocks")
    peers = route.get("peer_count")
    return (
        route.get("status") == "complete"
        and isinstance(covered, int)
        and not isinstance(covered, bool)
        and isinstance(total, int)
        and not isinstance(total, bool)
        and covered == total
        and isinstance(peers, int)
        and not isinstance(peers, bool)
        and peers > 0
    )


def _wait_for_selection(
    status_reader: Callable[[], Mapping[str, Any]],
    *,
    expected_digest: str,
    primary_complete: bool,
    timeout: float,
    poll_interval: float,
) -> tuple[Mapping[str, Any], int]:
    started = time.monotonic()
    deadline = started + timeout
    while True:
        status = status_reader()
        selection = status.get("auto_selection")
        primary = _route(status, PRIMARY_DIGEST)
        standby = _route(status, STANDBY_DIGEST)
        if (
            isinstance(selection, dict)
            and selection.get("status") == "selected"
            and selection.get("manifest_digest") == expected_digest
            and _route_complete(primary) is primary_complete
            and _route_complete(standby)
        ):
            return status, round((time.monotonic() - started) * 1000)
        if time.monotonic() >= deadline:
            raise AcceptanceError("selection_timeout")
        time.sleep(poll_interval)


def _one_token_completion(api_base: str, api_key: str, expected_model: str) -> Mapping[str, Any]:
    started = time.monotonic()
    response = _request_json(
        f"{api_base}/v1/completions",
        bearer=api_key,
        method="POST",
        body={"model": "auto", "prompt": ".", "max_tokens": 1, "temperature": 0},
    )
    choices = response.get("choices")
    usage = response.get("usage")
    if (
        response.get("model") != expected_model
        or not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
        or not isinstance(usage, dict)
        or usage.get("completion_tokens") != 1
    ):
        raise AcceptanceError("inference_response")
    return {
        "succeeded": True,
        "model": expected_model,
        "completion_tokens": 1,
        "finish_reason": choices[0].get("finish_reason"),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "output_retained": False,
    }


def run_acceptance(
    *,
    api_base: str,
    api_key: str,
    control_key: str,
    worker_id: str,
    timeout: float,
    poll_interval: float,
) -> Mapping[str, Any]:
    control_base = f"{api_base}/control/v1"

    def status_reader() -> Mapping[str, Any]:
        return _request_json(f"{control_base}/status", bearer=control_key, timeout=30)

    def worker_action(action: str) -> None:
        _request_json(
            f"{control_base}/workers/{worker_id}/{action}",
            bearer=control_key,
            method="POST",
            timeout=30,
        )

    stage = "initial_routes"
    paused = False
    started = time.monotonic()
    try:
        initial, initial_wait_ms = _wait_for_selection(
            status_reader,
            expected_digest=PRIMARY_DIGEST,
            primary_complete=True,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        primary_model = initial["auto_selection"]["model"]
        standby_model = next(
            model["id"] for model in initial["models"] if model.get("manifest_digest") == STANDBY_DIGEST
        )

        stage = "primary_inference"
        primary_inference = _one_token_completion(api_base, api_key, primary_model)

        stage = "primary_disable"
        worker_action("pause")
        paused = True
        fallback, fallback_wait_ms = _wait_for_selection(
            status_reader,
            expected_digest=STANDBY_DIGEST,
            primary_complete=False,
            timeout=timeout,
            poll_interval=poll_interval,
        )

        stage = "standby_inference"
        standby_inference = _one_token_completion(api_base, api_key, standby_model)

        stage = "primary_restore"
        worker_action("start")
        paused = False
        restored, restore_wait_ms = _wait_for_selection(
            status_reader,
            expected_digest=PRIMARY_DIGEST,
            primary_complete=True,
            timeout=timeout,
            poll_interval=poll_interval,
        )

        stage = "restored_inference"
        restored_inference = _one_token_completion(api_base, api_key, primary_model)
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": "gate11-product-node-acceptance",
            "result": "passed",
            "primary": {
                "manifest_digest": PRIMARY_DIGEST,
                "blocks": _route(restored, PRIMARY_DIGEST)["total_blocks"],
                "peer_count": _route(restored, PRIMARY_DIGEST)["peer_count"],
                "inference": primary_inference,
            },
            "standby": {
                "manifest_digest": STANDBY_DIGEST,
                "blocks": _route(fallback, STANDBY_DIGEST)["total_blocks"],
                "peer_count": _route(fallback, STANDBY_DIGEST)["peer_count"],
                "inference": standby_inference,
            },
            "fallback": {
                "primary_disabled": True,
                "automatic_selection": STANDBY_DIGEST,
                "selection_wait_ms": fallback_wait_ms,
            },
            "restoration": {
                "primary_restored": True,
                "automatic_selection": PRIMARY_DIGEST,
                "selection_wait_ms": restore_wait_ms,
                "inference": restored_inference,
            },
            "initial_selection_wait_ms": initial_wait_ms,
            "total_duration_ms": round((time.monotonic() - started) * 1000),
            "prompts_retained": False,
            "outputs_retained": False,
            "credentials_retained_in_evidence": False,
        }
    except BaseException:
        if paused:
            try:
                worker_action("start")
            except BaseException:
                pass
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": "gate11-product-node-acceptance",
            "result": "failed",
            "failure_stage": stage,
            "prompts_retained": False,
            "outputs_retained": False,
            "credentials_retained_in_evidence": False,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Gate 11 product-node acceptance probe")
    parser.add_argument("--api-base", default="http://127.0.0.1:8081")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--control-key-file", type=Path, required=True)
    parser.add_argument("--worker-id", default="automatic")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--poll-interval", type=float, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    evidence = run_acceptance(
        api_base=args.api_base.rstrip("/"),
        api_key=_read_secret(args.api_key_file),
        control_key=_read_secret(args.control_key_file),
        worker_id=args.worker_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    print(json.dumps(evidence, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0 if evidence["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
