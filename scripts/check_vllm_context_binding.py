"""Subsecond bound: vLLM launch, probe and text limits share one context."""

# isort: skip_file

import asyncio
import sys
import tempfile
import types
from pathlib import Path

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import Availability, ProviderIdentity, ProviderProfile  # noqa: E402
from drift.managed_vllm import ManagedVllmAdapter, ManagedVllmBinding  # noqa: E402
from drift.managed_vllm_text import ManagedVllmTextClient  # noqa: E402
from drift.managed_vllm_probe import probe_managed_vllm  # noqa: E402
from drift.text_request import RequestContext  # noqa: E402


async def main():
    profile = ProviderProfile("test/small-vllm-profile", "test/small-vllm", Availability.AVAILABLE,
                              qualification_id="a" * 64)
    binding = ManagedVllmBinding(
        profile, profile.model_id, "http://127.0.0.1:18247", "key", (0,), 1, 1,
        max_model_len=256,
    )
    with tempfile.TemporaryDirectory() as temporary:
        command, _ = binding.launch_spec(Path(temporary), max_model_len=256)
        assert command[command.index("--max-model-len") + 1] == "256"
        try:
            binding.launch_spec(Path(temporary), max_model_len=2048)
        except ValueError:
            pass
        else:
            raise AssertionError("launch context disagreed with route binding")
    try:
        await probe_managed_vllm(binding, expected_max_model_len=2048)
    except ValueError:
        pass
    else:
        raise AssertionError("probe context disagreed with route binding")
    bridge = ManagedVllmTextClient(
        ManagedVllmAdapter(binding), ProviderIdentity("provider", "instance"), "sha256:" + "c" * 64,
    )
    try:
        async for _ in bridge.stream(
            {"model": "sha256:" + "c" * 64, "prompt": "hi", "max_tokens": 256},
            chat=False, context=RequestContext.start(5),
        ):
            pass
    except ValueError:
        pass
    else:
        raise AssertionError("bridge accepted output beyond its served context")
    print("PASS: vLLM launch and bridge share 256-token context")


if __name__ == "__main__":
    asyncio.run(main())
