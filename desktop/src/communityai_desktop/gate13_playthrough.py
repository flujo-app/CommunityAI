"""Automate the real packaged Gate 13 desktop playthrough.

This module is deliberately part of the frozen desktop rather than an external UI
mock.  Qualification invocations reproduce the platform-specific sequence from the
accepted manual run using the normal window, real sharing-policy dialog, literal
Start/Pause buttons, and bounded localhost inference with an ephemeral client key.
It retains only bounded acceptance facts.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from communityai_desktop.client import normalize_loopback_url

SCHEMA_VERSION = 2
SCOPE = "gate13-packaged-desktop-playthrough"
MAX_CONFIG_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
QUALIFICATION_KEY_LABEL = "Gate 13 automated qualification"
MANUAL_ROUTE_WAIT_SECONDS = 450.0
MANUAL_ROUTE_POLL_SECONDS = 5.0

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MODEL_RE = re.compile(r"[ -~]{1,128}")
_POLICY_FIELDS = {
    "sharing_enabled",
    "allowed_models",
    "preferred_models",
    "denied_models",
    "max_disk_space",
    "max_vram",
    "max_bandwidth_mbps",
    "max_power_watts",
    "pause_timeout",
    "schedule",
}


def _manual_schedule() -> dict[str, Any]:
    return {
        "timezone": "UTC",
        "windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "00:00",
                "end": "23:59",
            }
        ],
    }


_CONFIG_FIELDS = {
    "schema_version",
    "run_id",
    "platform",
    "stage",
    "model_id",
    "manifest_digest",
    "total_blocks",
    "policy",
    "timeout_seconds",
    "inference_timeout_seconds",
}


class PlaythroughError(ValueError):
    """A qualification plan or observed desktop state failed closed."""


def _reject_constant(_value: str) -> None:
    raise PlaythroughError("configuration contains a non-finite value")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PlaythroughError("configuration contains a duplicate field")
        value[key] = item
    return value


def _regular_bytes(path: Path, maximum: int) -> bytes:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PlaythroughError("configuration is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise PlaythroughError("configuration is not a bounded regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PlaythroughError("configuration is unreadable") from exc


def _bounded_number(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise PlaythroughError(f"{label} is invalid")
    rendered = float(value)
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        raise PlaythroughError(f"{label} is invalid")
    return rendered


def _selectors(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise PlaythroughError(f"{label} is invalid")
    clean: list[str] = []
    folded: set[str] = set()
    for item in value:
        if not isinstance(item, str) or _MODEL_RE.fullmatch(item) is None or item != item.strip():
            raise PlaythroughError(f"{label} is invalid")
        canonical = item.casefold()
        if canonical in folded:
            raise PlaythroughError(f"{label} contains a duplicate selector")
        folded.add(canonical)
        clean.append(item)
    return tuple(clean)


def _policy(value: Any, model_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _POLICY_FIELDS:
        raise PlaythroughError("sharing policy schema is invalid")
    allowed = _selectors(value["allowed_models"], "allowed models")
    preferred = _selectors(value["preferred_models"], "preferred models")
    denied = _selectors(value["denied_models"], "denied models")
    if value["sharing_enabled"] is not True:
        raise PlaythroughError("sharing must be enabled for the start stage")
    if model_id not in allowed or model_id not in preferred or denied:
        raise PlaythroughError("sharing policy does not select the qualification model")
    if value["max_disk_space"] != "32GB":
        raise PlaythroughError("storage ceiling does not match the proven manual replay")
    if value["max_vram"] != "20GB":
        raise PlaythroughError("memory ceiling does not match the proven manual replay")
    bandwidth = _bounded_number(value["max_bandwidth_mbps"], "bandwidth ceiling", minimum=0.001, maximum=1_000_000)
    if bandwidth != 100.0:
        raise PlaythroughError("bandwidth ceiling does not match the proven manual replay")
    if value["max_power_watts"] is not None:
        raise PlaythroughError("the manual CPU-host replay requires an unset power ceiling")
    pause = _bounded_number(value["pause_timeout"], "pause timeout", minimum=1, maximum=300)
    if pause != 120.0:
        raise PlaythroughError("pause timeout does not match the proven manual replay")
    if value["schedule"] != _manual_schedule():
        raise PlaythroughError("sharing schedule does not match the proven manual replay")
    return {
        "sharing_enabled": True,
        "allowed_models": list(allowed),
        "preferred_models": list(preferred),
        "denied_models": list(denied),
        "max_disk_space": value["max_disk_space"],
        "max_vram": value["max_vram"],
        "max_bandwidth_mbps": bandwidth,
        "max_power_watts": None,
        "pause_timeout": pause,
        "schedule": _manual_schedule(),
    }


@dataclass(frozen=True)
class PlaythroughPlan:
    run_id: str
    platform: str
    stage: str
    model_id: str
    manifest_digest: str
    total_blocks: int
    policy: Mapping[str, Any]
    timeout_seconds: float
    inference_timeout_seconds: float

    @classmethod
    def load(cls, path: Path) -> "PlaythroughPlan":
        payload = _regular_bytes(path, MAX_CONFIG_BYTES)
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlaythroughError("configuration is invalid JSON") from exc
        if not isinstance(raw, dict) or set(raw) != _CONFIG_FIELDS or raw.get("schema_version") != SCHEMA_VERSION:
            raise PlaythroughError("configuration schema is invalid")
        run_id = raw["run_id"]
        platform = raw["platform"]
        stage = raw["stage"]
        model_id = raw["model_id"]
        digest = raw["manifest_digest"]
        total_blocks = raw["total_blocks"]
        if not isinstance(run_id, str) or _RUN_RE.fullmatch(run_id) is None:
            raise PlaythroughError("run id is invalid")
        if platform not in ("windows", "linux"):
            raise PlaythroughError("playthrough platform is invalid")
        if stage not in ("initial", "restart"):
            raise PlaythroughError("playthrough stage is invalid")
        if not isinstance(model_id, str) or _MODEL_RE.fullmatch(model_id) is None or model_id != model_id.strip():
            raise PlaythroughError("model id is invalid")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise PlaythroughError("manifest digest is invalid")
        if type(total_blocks) is not int or not 1 <= total_blocks <= 512:
            raise PlaythroughError("block count is invalid")
        timeout = _bounded_number(raw["timeout_seconds"], "playthrough timeout", minimum=30, maximum=3_600)
        inference_timeout = _bounded_number(
            raw["inference_timeout_seconds"], "inference timeout", minimum=10, maximum=600
        )
        return cls(
            run_id=run_id,
            platform=platform,
            stage=stage,
            model_id=model_id,
            manifest_digest=digest,
            total_blocks=total_blocks,
            policy=_policy(raw["policy"], model_id),
            timeout_seconds=timeout,
            inference_timeout_seconds=inference_timeout,
        )


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


def _completion_request(url: str, secret: str, timeout: float) -> Mapping[str, Any]:
    body = json.dumps(
        {
            "model": "auto",
            "messages": [{"role": "user", "content": "Reply with one word."}],
            "max_tokens": 1,
            "stream": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise PlaythroughError("localhost inference was rejected")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise PlaythroughError("localhost inference failed") from exc
    if not 1 <= len(payload) <= MAX_RESPONSE_BYTES:
        raise PlaythroughError("localhost inference response is invalid")
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaythroughError("localhost inference response is invalid") from exc
    if not isinstance(value, dict):
        raise PlaythroughError("localhost inference response is invalid")
    return value


def _manual_route_ready(status: Mapping[str, Any], plan: PlaythroughPlan) -> bool:
    selection = status.get("auto_selection")
    models = status.get("models")
    if not isinstance(selection, dict) or not isinstance(models, list):
        return False
    selected = next(
        (
            item
            for item in models
            if isinstance(item, dict)
            and item.get("id") == plan.model_id
            and item.get("manifest_digest") == plan.manifest_digest
        ),
        None,
    )
    return bool(
        selection.get("status") == "selected"
        and selection.get("model") == plan.model_id
        and selection.get("manifest_digest") == plan.manifest_digest
        and selection.get("covered_blocks") == plan.total_blocks
        and selection.get("total_blocks") == plan.total_blocks
        and isinstance(selection.get("peer_count"), int)
        and selection["peer_count"] > 0
        and selected is not None
        and selected.get("route_complete") is True
        and selected.get("covered_blocks") == plan.total_blocks
        and selected.get("total_blocks") == plan.total_blocks
    )


def _completion_after_manual_readiness_wait(
    controller: Any,
    plan: PlaythroughPlan,
    url: str,
    secret: str,
) -> Mapping[str, Any]:
    """Replay the manual Model-unavailable -> wait-for-complete -> retry sequence."""

    try:
        return _completion_request(url, secret, plan.inference_timeout_seconds)
    except PlaythroughError as first_error:
        deadline = time.monotonic() + min(MANUAL_ROUTE_WAIT_SECONDS, plan.inference_timeout_seconds)
        while time.monotonic() < deadline:
            time.sleep(min(MANUAL_ROUTE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            try:
                status = controller.client.status()
            except BaseException:
                continue
            if _manual_route_ready(status, plan):
                try:
                    return _completion_request(url, secret, plan.inference_timeout_seconds)
                except PlaythroughError:
                    raise first_error
        raise first_error


def qualify_localhost_inference(controller: Any, plan: PlaythroughPlan) -> dict[str, Any]:
    """Run one response-content-free localhost inference and restore the API-key baseline."""

    baseline_items = [item for item in controller.client.list_keys() if item.get("revoked_at") is None]
    baseline = {item["id"] for item in baseline_items}
    if not baseline:
        raise PlaythroughError("a preexisting client key is required")
    if any(item.get("label") == QUALIFICATION_KEY_LABEL for item in baseline_items):
        raise PlaythroughError("a prior qualification key remains active")
    created_id = ""
    secret = ""
    failed = False
    cleanup_failed = False
    completion_count = 0
    generated_token_count = 0
    try:
        status = controller.client.status()
        selection = status["auto_selection"]
        if (
            selection.get("status") != "selected"
            or selection.get("model") != plan.model_id
            or selection.get("manifest_digest") != plan.manifest_digest
        ):
            raise PlaythroughError("automatic selection changed before inference")
        created = controller.client.create_key(QUALIFICATION_KEY_LABEL)
        created_id = created.get("key", {}).get("id", "")
        secret = created.get("secret", "")
        if (
            not isinstance(created_id, str)
            or not created_id
            or not isinstance(secret, str)
            or not 1 <= len(secret) <= 512
        ):
            raise PlaythroughError("temporary client key response is invalid")
        base = normalize_loopback_url(status["openai_base_url"])
        completion = _completion_after_manual_readiness_wait(
            controller,
            plan,
            f"{base}/v1/chat/completions",
            secret,
        )
        if completion.get("model") != plan.model_id:
            raise PlaythroughError("localhost inference identity is invalid")
        usage = completion.get("usage")
        generated = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if type(generated) is not int or generated != 1:
            raise PlaythroughError("localhost inference token count is invalid")
        completion_count = 1
        generated_token_count = generated
    except BaseException:
        failed = True
    finally:
        secret = ""
        try:
            active = {item["id"] for item in controller.client.list_keys() if item.get("revoked_at") is None}
            candidates = active - baseline
            if created_id and created_id in active:
                candidates.add(created_id)
            if len(candidates) != 1:
                raise PlaythroughError("temporary client key identity is ambiguous")
            controller.client.revoke_key(next(iter(candidates)))
            after = {item["id"] for item in controller.client.list_keys() if item.get("revoked_at") is None}
            if after != baseline:
                raise PlaythroughError("temporary client key cleanup failed")
        except BaseException:
            cleanup_failed = True
    if failed or cleanup_failed:
        raise PlaythroughError("localhost inference or key cleanup failed")
    return {
        "passed": True,
        "model_id": plan.model_id,
        "manifest_digest": plan.manifest_digest,
        "completion_count": completion_count,
        "generated_token_count": generated_token_count,
        "response_content_retained": False,
        "token_identifiers_retained": False,
        "temporary_key_removed": True,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (destination.is_symlink() or not destination.is_file()):
        raise PlaythroughError("evidence destination is unsafe")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".gate13-playthrough-", suffix=".tmp", dir=destination.parent, delete=False
        ) as out:
            temporary_name = out.name
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary_name, destination)
        temporary_name = ""
    except OSError as exc:
        raise PlaythroughError("evidence could not be persisted") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


class Gate13Playthrough:
    """A bounded Qt state machine that drives the real packaged controls."""

    def __init__(
        self,
        plan: PlaythroughPlan,
        evidence_path: Path,
        *,
        screenshot_path: Path | None = None,
        inference_runner: Callable[[Any, PlaythroughPlan], Mapping[str, Any]] = qualify_localhost_inference,
        clock: Callable[[], float] = time.monotonic,
        start_observation_seconds: float | None = None,
        restart_observation_seconds: float | None = None,
    ):
        self.plan = plan
        self.evidence_path = Path(evidence_path)
        self.screenshot_path = None if screenshot_path is None else Path(screenshot_path)
        self._inference_runner = inference_runner
        self._clock = clock
        default_start_observation = 25.0 if plan.platform == "windows" else 20.0
        self._start_observation_seconds = (
            default_start_observation
            if start_observation_seconds is None
            else _bounded_number(start_observation_seconds, "start observation", minimum=0.05, maximum=60)
        )
        self._restart_observation_seconds = (
            15.0
            if restart_observation_seconds is None
            else _bounded_number(restart_observation_seconds, "restart observation", minimum=0.05, maximum=60)
        )
        self._started = clock()
        self._state = "wait_ready"
        self._done = False
        self._inference: Mapping[str, Any] | None = None
        self._window = None
        self._application = None
        self._qt: Mapping[str, Any] = {}
        self._timer = None
        self._observation_deadline: float | None = None
        self._ui = {
            "real_window_opened": True,
            "policy_dialog_saved": False,
            "start_clicked": False,
            "pause_control_observed": False,
            "pause_clicked": False,
            "restart_resume_observed": False,
            "sharing_intent_enabled_observed": False,
            "sharing_intent_disabled_observed": False,
        }

    def install(self, window: Any, application: Any, qt: Mapping[str, Any]) -> None:
        self._window = window
        self._application = application
        self._qt = qt
        timer_type = qt["QTimer"]
        self._timer = timer_type(window)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        timer_type.singleShot(max(1, int(self.plan.timeout_seconds * 1_000)), self._timeout)

    def _timeout(self) -> None:
        if not self._done:
            self._fail()

    def _ready(self) -> bool:
        window = self._window
        if window is None or window._controller is None or window._busy:
            return False
        snapshot = window._snapshot
        selection = snapshot.get("auto_selection", {})
        models = snapshot.get("models", [])
        selected = next((item for item in models if item.get("id") == self.plan.model_id), None)
        return bool(
            selection.get("status") == "selected"
            and selection.get("model") == self.plan.model_id
            and selection.get("manifest_digest") == self.plan.manifest_digest
            and selection.get("covered_blocks") == self.plan.total_blocks
            and selection.get("total_blocks") == self.plan.total_blocks
            and isinstance(selection.get("peer_count"), int)
            and selection["peer_count"] > 0
            and selected is not None
            and selected.get("route_complete") is True
            and selected.get("covered_blocks") == self.plan.total_blocks
            and selected.get("total_blocks") == self.plan.total_blocks
        )

    def _tick(self) -> None:
        if self._done or self._window is None:
            return
        try:
            if self._state == "wait_ready":
                if not self._ready():
                    return
                if self.plan.stage == "initial":
                    self._begin_inference("after_initial_inference")
                elif self.plan.platform == "windows":
                    self._begin_policy_edit()
                else:
                    if not self._show_sharing_page():
                        self._fail()
                        return
                    self._state = "wait_resumed"
            elif self._state == "wait_policy":
                contribution = self._window._snapshot.get("contribution", {})
                if not self._window._busy and contribution.get("policy") == self.plan.policy:
                    self._ui["policy_dialog_saved"] = True
                    self._click_start()
            elif self._state == "wait_started_intent":
                contribution = self._window._snapshot.get("contribution", {})
                if contribution.get("intent_enabled") and self._pause_control_available():
                    self._ui["sharing_intent_enabled_observed"] = True
                    self._ui["pause_control_observed"] = True
                    if self._observation_deadline is None:
                        self._observation_deadline = self._clock() + self._start_observation_seconds
                    if self._clock() >= self._observation_deadline:
                        if self.plan.platform == "windows":
                            self._click_pause()
                        else:
                            self._pass()
            elif self._state == "wait_resumed":
                contribution = self._window._snapshot.get("contribution", {})
                workers = self._window._snapshot.get("workers", [])
                desired = any(
                    item.get("model") == self.plan.model_id and item.get("desired_running") for item in workers
                )
                if contribution.get("intent_enabled") and desired and self._pause_control_available():
                    self._ui["sharing_intent_enabled_observed"] = True
                    self._ui["pause_control_observed"] = True
                    self._ui["restart_resume_observed"] = True
                    if self._observation_deadline is None:
                        self._observation_deadline = self._clock() + self._restart_observation_seconds
                    if self._clock() >= self._observation_deadline:
                        self._click_pause()
            elif self._state == "wait_paused_intent":
                contribution = self._window._snapshot.get("contribution", {})
                if (
                    not self._window._busy
                    and not contribution.get("intent_enabled")
                    and self._start_control_available()
                ):
                    self._ui["sharing_intent_disabled_observed"] = True
                    if self.plan.platform == "linux":
                        self._begin_inference("after_restart_inference")
                    else:
                        self._pass()
        except BaseException:
            self._fail()

    def _begin_inference(self, waiting_state: str) -> None:
        self._state = waiting_state
        controller = self._window._controller

        def finished(result: Mapping[str, Any]) -> None:
            self._inference = dict(result)
            if waiting_state == "after_initial_inference" and self.plan.platform == "linux":
                self._begin_policy_edit()
            else:
                self._pass()

        self._window._submit(
            lambda: self._inference_runner(controller, self.plan),
            finished,
            lambda _message: self._fail(),
        )

    def _begin_policy_edit(self) -> None:
        if not self._show_sharing_page():
            self._fail()
            return
        if self._window.edit_policy_button.isEnabled() is False:
            self._fail()
            return
        self._state = "editing_policy"
        self._qt["QTimer"].singleShot(100, self._fill_policy_dialog)
        self._window.edit_policy_button.click()

    def _fill_policy_dialog(self) -> None:
        try:
            dialog = self._window.findChild(self._qt["QDialog"], "sharingPolicyDialog")
            if dialog is None:
                raise PlaythroughError("sharing policy dialog did not open")
            checkbox = dialog.findChild(self._qt["QCheckBox"], "policy_sharing_enabled")
            checkbox.setChecked(True)
            for field in ("allowed_models", "preferred_models", "denied_models"):
                editor = dialog.findChild(self._qt["QPlainTextEdit"], f"policy_{field}")
                editor.setPlainText("\n".join(self.plan.policy[field]))
            for field in (
                "max_disk_space",
                "max_vram",
                "max_bandwidth_mbps",
                "max_power_watts",
                "pause_timeout",
            ):
                editor = dialog.findChild(self._qt["QLineEdit"], f"policy_{field}")
                value = self.plan.policy[field]
                editor.setText("" if value is None else f"{value:g}" if isinstance(value, float) else str(value))
            schedule = dialog.findChild(self._qt["QPlainTextEdit"], "policy_schedule")
            schedule.setPlainText(json.dumps(self.plan.policy["schedule"], separators=(",", ":")))
            buttons = dialog.findChild(self._qt["QDialogButtonBox"], "sharingPolicyButtons")
            save = buttons.button(self._qt["QDialogButtonBox"].StandardButton.Save)
            self._state = "wait_policy"
            save.click()
        except BaseException:
            self._fail()

    def _show_sharing_page(self) -> bool:
        window = self._window
        buttons = None if window is None else getattr(window, "_page_buttons", None)
        if not isinstance(buttons, list) or len(buttons) != 4:
            return False
        button = buttons[2]
        if button.text() != "Sharing" or not button.isEnabled():
            return False
        if not button.isChecked():
            button.click()
        return bool(button.isChecked())

    def _click_start(self) -> None:
        if not self._show_sharing_page():
            self._fail()
            return
        button = self._window.master_share_button
        if button.text() != "Start sharing" or not button.isEnabled():
            return
        self._state = "wait_started_intent"
        self._observation_deadline = None
        self._ui["start_clicked"] = True
        button.click()

    def _click_pause(self) -> None:
        if not self._show_sharing_page():
            self._fail()
            return
        button = self._window.master_share_button
        if button.text() != "Pause sharing" or not button.isEnabled():
            return
        self._state = "wait_paused_intent"
        self._observation_deadline = None
        self._ui["pause_clicked"] = True
        button.click()

    def _pause_control_available(self) -> bool:
        button = self._window.master_share_button
        return bool(button.text() == "Pause sharing" and button.isEnabled())

    def _start_control_available(self) -> bool:
        button = self._window.master_share_button
        # A just-paused worker may remain temporarily resource-suspended while its
        # process exits, which legitimately leaves Start disabled.  The manual run
        # accepted the intent transition and literal control text at this boundary.
        return bool(button.text() == "Start sharing")

    def _base_result(self, result: str) -> dict[str, Any]:
        duration = max(0.0, self._clock() - self._started)
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": SCOPE,
            "run_id": self.plan.run_id,
            "platform": self.plan.platform,
            "stage": self.plan.stage,
            "result": result,
            "model_id": self.plan.model_id,
            "manifest_digest": self.plan.manifest_digest,
            "duration_seconds": round(duration, 6),
        }

    def _pass(self) -> None:
        inference_required = (self.plan.platform, self.plan.stage) in {
            ("windows", "initial"),
            ("linux", "initial"),
            ("linux", "restart"),
        }
        if self._done or (inference_required and self._inference is None):
            self._fail()
            return
        value = self._base_result("passed")
        value.update(
            {
                "route": {
                    "rendered_in_real_window": True,
                    "complete": True,
                    "covered_blocks": self.plan.total_blocks,
                    "total_blocks": self.plan.total_blocks,
                },
                "inference": None if self._inference is None else dict(self._inference),
                "ui": dict(self._ui),
                "limits": {
                    "storage": self._ui["policy_dialog_saved"],
                    "memory_or_vram": self._ui["policy_dialog_saved"],
                    "bandwidth": self._ui["policy_dialog_saved"],
                    "power": False,
                    "pause_timeout": self._ui["policy_dialog_saved"],
                    "schedule": self._ui["policy_dialog_saved"],
                },
                "timing": {
                    "start_observation_seconds": (
                        self._start_observation_seconds if self._ui["start_clicked"] else 0.0
                    ),
                    "restart_observation_seconds": (
                        self._restart_observation_seconds if self._ui["restart_resume_observed"] else 0.0
                    ),
                },
                "privacy": {
                    "prompt_retained": False,
                    "response_content_retained": False,
                    "token_identifiers_retained": False,
                    "credentials_retained": False,
                    "paths_retained": False,
                    "endpoints_retained": False,
                },
            }
        )
        self._finish(value)

    def _fail(self) -> None:
        if self._done:
            return
        value = self._base_result("failed")
        value["failure_code"] = "playthrough_failed"
        self._finish(value)

    def _finish(self, value: Mapping[str, Any]) -> None:
        self._done = True
        if self._timer is not None:
            self._timer.stop()
        try:
            if self.screenshot_path is not None and self._window is not None:
                self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                if not self._window.grab().save(str(self.screenshot_path)):
                    raise PlaythroughError("playthrough screenshot failed")
            _atomic_json(self.evidence_path, value)
        except BaseException:
            fallback = self._base_result("failed")
            fallback["failure_code"] = "evidence_write_failed"
            try:
                _atomic_json(self.evidence_path, fallback)
            except BaseException:
                pass
        finally:
            if self._application is not None:
                self._application.quit()
