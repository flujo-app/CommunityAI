"""Synthetic credit reservation around a qualified test text provider.

This bridge has no live processor, cash balance, independent fraud decision or
production route. It exercises admission/settlement in the local simulator.
"""

from __future__ import annotations

import hashlib
import json
from typing import AsyncIterator, Callable

from drift.commerce_simulator import CommerceSimulationError, CommerceSimulator, SimulatedServiceQuote
from drift.managed_vllm_text import ManagedVllmTextClient
from drift.text_request import RequestContext


class SimulatedPaidTextClient:
    """Reserve a quoted test balance before inference; settle accepted usage."""

    supports_request_context = True

    def __init__(
        self,
        delegate: ManagedVllmTextClient,
        journal: CommerceSimulator,
        quote_for_request: Callable[[dict, bool, RequestContext], SimulatedServiceQuote],
    ) -> None:
        if (
            type(delegate) is not ManagedVllmTextClient
            or type(journal) is not CommerceSimulator
            or not callable(quote_for_request)
        ):
            raise ValueError("invalid simulated paid text binding")
        self.delegate = delegate
        self.journal = journal
        self.quote_for_request = quote_for_request

    async def stream(self, body: dict, *, chat: bool, context: RequestContext) -> AsyncIterator[dict]:
        if type(body) is not dict or type(chat) is not bool or type(context) is not RequestContext:
            raise ValueError("invalid simulated paid request")
        if context.caller_id is None:
            raise ValueError("simulated paid request requires an identified buyer")
        context.require_live()
        quote = self.quote_for_request(body, chat, context)
        if type(quote) is not SimulatedServiceQuote:
            raise ValueError("simulated paid request requires one immutable quote")
        profile = self.delegate.adapter.binding.profile
        if (
            quote.request_id != context.request_id
            or quote.buyer_id != context.caller_id
            or quote.provider_id != self.delegate.identity.provider_id
            or quote.profile_id != profile.profile_id
            or quote.model_id != profile.model_id
            or quote.artifact_sha256 != self.delegate.manifest_digest[7:]
            or body.get("model") != self.delegate.manifest_digest
        ):
            raise ValueError("simulated quote and provider route disagree")
        requested_output = body.get("max_tokens")
        if chat:
            alternate = body.get("max_completion_tokens")
            if requested_output is not None and alternate is not None and requested_output != alternate:
                raise ValueError("conflicting simulated output limits")
            requested_output = alternate if requested_output is None else requested_output
        requested_output = 512 if requested_output is None else requested_output
        if type(requested_output) is not int or not 1 <= requested_output <= quote.max_output_units:
            raise ValueError("simulated quote does not cover requested output")

        if not self.journal.reserve_service(quote):
            raise CommerceSimulationError("simulated request ID already reserved")
        settled = False
        output_hash = hashlib.sha256()
        delegate_stream = self.delegate.stream(body, chat=chat, context=context)
        try:
            async for frame in delegate_stream:
                if type(frame) is not dict:
                    raise ValueError("invalid simulated provider frame")
                kind = frame.get("type")
                if settled:
                    raise ValueError("provider emitted data after simulated settlement")
                if kind == "delta":
                    piece = frame.get("text")
                    if type(piece) is not str:
                        raise ValueError("invalid simulated output")
                    output_hash.update(piece.encode("utf-8"))
                elif kind == "done":
                    usage = frame.get("usage")
                    if type(usage) is not dict:
                        raise ValueError("missing simulated usage")
                    input_units = usage.get("prompt_tokens")
                    output_units = usage.get("completion_tokens")
                    total_units = usage.get("total_tokens")
                    if (
                        type(input_units) is not int or type(output_units) is not int
                        or type(total_units) is not int or input_units < 0 or output_units < 0
                        or total_units != input_units + output_units
                    ):
                        raise ValueError("invalid simulated usage")
                    charge = input_units * quote.input_unit_price + output_units * quote.output_unit_price
                    if charge <= 0:
                        raise ValueError("zero priced simulated completion")
                    fee = charge * quote.fee_bps // 10_000
                    receipt = hashlib.sha256(json.dumps(
                        [quote.digest, usage, frame.get("finish_reason"), output_hash.hexdigest()],
                        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                    ).encode("ascii")).hexdigest()
                    if not self.journal.settle_service(
                        quote.request_id, quote.provider_id, charge, fee,
                        "simdecision:" + quote.request_id, receipt, input_units, output_units,
                    ):
                        raise CommerceSimulationError("simulated settlement already applied")
                    settled = True
                elif kind != "heartbeat":
                    raise ValueError("unsupported simulated provider frame")
                yield frame
            if not settled:
                raise ValueError("simulated provider ended without completion")
        finally:
            try:
                await delegate_stream.aclose()
            finally:
                if not settled:
                    self.journal.refund_service(quote.request_id, "aborted")
