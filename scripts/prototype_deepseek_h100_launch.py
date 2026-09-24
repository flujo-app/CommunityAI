"""Standalone argv experiment for the pinned DeepSeek H100 recipe; no GPU or vLLM."""

RECIPE_ARGS = (
    "--language-model-only",
    "--tokenizer-mode",
    "deepseek_v41",
    "--engram-config",
    '{"cpu_offload":true}',
    "--max-num-batched-tokens",
    "4096",
    "--gpu-memory-utilization",
    "0.92",
)


def candidate(model: str, devices: tuple[int, ...], host_ram_bytes: int) -> tuple[str, ...]:
    if model != "deepseek-ai/DeepSeek-V4.1-Flash":
        raise ValueError("wrong model")
    if len(devices) != 8 or len(set(devices)) != 8 or any(type(item) is not int or item < 0 for item in devices):
        raise ValueError("H100 recipe requires eight distinct devices")
    if type(host_ram_bytes) is not int or host_ram_bytes < 183 * (1 << 30):
        raise ValueError("Engram offload needs 183 GiB free host RAM")
    return ("vllm", "serve", "/verified/model", "--tensor-parallel-size", "8") + RECIPE_ARGS


if __name__ == "__main__":
    command = candidate("deepseek-ai/DeepSeek-V4.1-Flash", tuple(range(8)), 200 * (1 << 30))
    assert "--engram-config" in command and "--language-model-only" in command
    for kwargs in (
        ("zai-org/GLM-5.3", tuple(range(8)), 200 * (1 << 30)),
        ("deepseek-ai/DeepSeek-V4.1-Flash", (0, 1), 200 * (1 << 30)),
        ("deepseek-ai/DeepSeek-V4.1-Flash", tuple(range(8)), 100 * (1 << 30)),
    ):
        try:
            candidate(*kwargs)
        except ValueError:
            continue
        raise AssertionError("unsafe launch candidate accepted")
    print("DeepSeek H100 standalone argv prototype PASS")
