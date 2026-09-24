"""Standalone request identity and budget experiment for the managed API seam."""

# isort: skip_file

import importlib.util
import sys
import time
import types
from pathlib import Path


PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import (  # noqa: E402
    Availability,
    InferenceLimits,
    InferenceRequest,
    ProviderIdentity,
    ProviderProfile,
)
from drift.text_request import RequestContext  # noqa: E402


def build(body, context, profile, identity):
    prompt = body.get("prompt")
    maximum = body.get("max_tokens", 512)
    if type(prompt) is not str or not prompt or type(maximum) is not int or not 1 <= maximum <= 512:
        raise ValueError("unsupported managed request")
    return InferenceRequest(
        identity,
        profile.profile_id,
        profile.model_id,
        context.request_id,
        "b" * 32,
        context.issued_at,
        context.deadline,
        prompt,
        InferenceLimits(2048 - maximum, maximum, 4096, 1 << 20),
    )


def main():
    context = RequestContext.start(5.0)
    profile = ProviderProfile("test/profile", "test/model", Availability.AVAILABLE, qualification_id="e" * 64)
    identity = ProviderIdentity("test/provider", "test/instance")
    result = build({"prompt": "hello", "max_tokens": 20}, context, profile, identity)
    assert result.request_id == context.request_id and result.deadline == context.deadline
    assert result.issued_at == context.issued_at and result.limits.max_output_units == 20
    assert result.limits.max_input_units + result.limits.max_output_units == 2048
    try:
        build({"prompt": "hello", "max_tokens": 513}, context, profile, identity)
        raise AssertionError("unbounded output accepted")
    except ValueError:
        pass
    assert time.monotonic() < result.deadline
    print("managed API bridge prototype: logical identity, shared deadline and output cap PASS")


if __name__ == "__main__":
    main()
