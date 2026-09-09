from types import SimpleNamespace
from unittest.mock import patch

import pytest
import torch

from drift.client.lm_head import LMHead


@pytest.mark.parametrize("supports_bf16", [False, True])
def test_auto_head_uses_torch_probe_without_importing_cpufeature(supports_bf16):
    config = SimpleNamespace(vocab_size=19, hidden_size=8, use_chunked_forward="auto", chunked_forward_step=7)
    with patch.dict("sys.modules", {"cpufeature": None}), patch.object(
        torch.cpu, "_is_avx512_bf16_supported", return_value=supports_bf16
    ):
        head = LMHead(config).to(dtype=torch.bfloat16)
    assert head.use_chunked_forward is not supports_bf16
    torch.manual_seed(3)
    head.weight.copy_(torch.randn_like(head.weight))
    inputs = torch.randn(1, 3, 8, dtype=torch.bfloat16)
    output = head(inputs)
    reference = torch.nn.functional.linear(inputs.float(), head.weight.float())
    assert torch.allclose(output.float(), reference, atol=0.03, rtol=0.01)
