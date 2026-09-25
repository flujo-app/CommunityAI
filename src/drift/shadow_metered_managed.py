"""Opt-in noncash test-credit admission for a local managed vLLM bridge.

Only synthetic test profiles are eligible. Backend usage creates an untrusted
pending claim; this module has no validation, settlement, cash, or payout role.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import AsyncIterator

from drift.managed_vllm_text import ManagedVllmTextClient
from drift.shadow_credits import ShadowLedger, ShadowQuote, WorkReceipt
from drift.text_request import RequestContext


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


class ShadowMeteredManagedClient:
    """Test-only wrapper that holds a quoted cap before contacting vLLM."""

    supports_request_context = True

    def __init__(self, bridge: ManagedVllmTextClient, ledger: ShadowLedger) -> None:
        if type(bridge) is not ManagedVllmTextClient or type(ledger) is not ShadowLedger:
            raise ValueError("invalid shadow metering inputs")
        if not bridge.adapter.binding.profile.profile_id.startswith("test/"):
            raise ValueError("shadow metering requires a synthetic test profile")
        self.bridge = bridge
        self.ledger = ledger
        self.provider_id = "provider_" + _hash(bridge.identity.provider_id)[:32]

    async def stream(self, body: dict, *, chat: bool, context: RequestContext) -> AsyncIterator[dict]:
        if type(context) is not RequestContext or context.caller_id is None:
            raise ValueError("shadow test credits require an identified API key")
        if type(body) is not dict or body.get("model") != self.bridge.manifest_digest:
            raise ValueError("managed manifest identity mismatch")
        maximum = body.get("max_tokens")
        if chat and body.get("max_completion_tokens") is not None:
            alternate = body["max_completion_tokens"]
            if maximum is not None and maximum != alternate:
                raise ValueError("conflicting managed output limits")
            maximum = alternate
        maximum = 512 if maximum is None else maximum
        if type(maximum) is not int or not 1 <= maximum <= 512:
            raise ValueError("managed output limit must be 1..512")
        context.require_live()
        profile = self.bridge.adapter.binding.profile
        quote = ShadowQuote(
            request_id=context.request_id,
            buyer_id=context.caller_id,
            model_id=profile.model_id,
            profile_id=profile.profile_id,
            # Synthetic profile's manifest hash is a fixture binding, not a
            # verified weight hash or real-model qualification claim.
            artifact_sha256=self.bridge.manifest_digest[7:],
            service_policy_sha256=_hash(["shadow-managed-v1", "text", 2048, 512]),
            price_schedule_sha256=_hash(["shadow-managed-v1", "test-credit", 1, 1, 0]),
            service_class="text_inference",
            settlement_domain="local_test",
            input_unit_price=1,
            output_unit_price=1,
            max_input_units=2048 - maximum,
            max_output_units=maximum,
            fee_bps=0,
            spend_cap=2048,
            expires_at_unix=int(time.time() + context.remaining()) + 2,
        )
        self.ledger.reserve(quote)
        iterator = self.bridge.stream(body, chat=chat, context=context)
        claimed = False
        try:
            async for frame in iterator:
                if frame.get("type") == "done":
                    usage = frame.get("usage")
                    if type(usage) is not dict:
                        raise ValueError("managed completion has no usage")
                    input_units = usage.get("prompt_tokens")
                    output_units = usage.get("completion_tokens")
                    if type(input_units) is not int or type(output_units) is not int:
                        raise ValueError("managed completion has invalid usage")
                    receipt = WorkReceipt(
                        receipt_id="receipt_" + context.request_id,
                        request_id=context.request_id,
                        provider_id=self.provider_id,
                        stage_id="text_generation",
                        attempt_id=context.attempt_id,
                        input_units=input_units,
                        output_units=output_units,
                        proposed_charge=input_units + output_units,
                        evidence_sha256=_hash(
                            [context.request_id, context.attempt_id, self.bridge.identity.instance_id,
                             input_units, output_units, frame.get("finish_reason")]
                        ),
                    )
                    self.ledger.submit_receipt(receipt)
                    claimed = True
                yield frame
        finally:
            try:
                await iterator.aclose()
            finally:
                if not claimed:
                    # No accepted completion may leave a buyer hold behind.
                    self.ledger.finalize(context.request_id)
