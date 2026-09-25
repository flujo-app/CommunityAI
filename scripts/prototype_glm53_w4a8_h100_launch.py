"""Standalone GLM-5.3 W4A8 H100 recipe experiment; builds no process."""

ARTIFACT = "gpustack/GLM-5.3-W4A8"
MINIMUM_GPU_BYTES = 447_000_000_000


def candidate(artifact: str, devices: tuple[int, ...], free_gpu_bytes: tuple[int, ...]):
    if artifact != ARTIFACT:
        raise ValueError("wrong artifact")
    if (
        type(devices) is not tuple or len(devices) != 8
        or any(type(device) is not int or device < 0 for device in devices)
        or len(set(devices)) != 8
        or type(free_gpu_bytes) is not tuple or len(free_gpu_bytes) != 8
        or any(type(amount) is not int or amount < (MINIMUM_GPU_BYTES + 7) // 8 for amount in free_gpu_bytes)
        or sum(free_gpu_bytes) < MINIMUM_GPU_BYTES
    ):
        raise ValueError("H100 W4A8 candidate requires eight distinct adequately free GPUs")
    return (
        "vllm", "serve", "/reviewed/snapshot", "--served-model-name", ARTIFACT,
        "--tensor-parallel-size", "8", "--enable-expert-parallel",
        "--kv-cache-dtype", "fp8_ds_mla", "--trust-remote-code",
    )


if __name__ == "__main__":
    command = candidate(ARTIFACT, tuple(range(8)), (70_000_000_000,) * 8)
    assert command[command.index("--tensor-parallel-size") + 1] == "8"
    assert "--enable-expert-parallel" in command and "--trust-remote-code" in command
    for arguments in (
        ("zai-org/GLM-5.3", tuple(range(8)), (70_000_000_000,) * 8),
        (ARTIFACT, tuple(range(7)), (70_000_000_000,) * 8),
        (ARTIFACT, (0,) * 8, (70_000_000_000,) * 8),
        (ARTIFACT, tuple(range(8)), (50_000_000_000,) * 8),
    ):
        try:
            candidate(*arguments)
        except ValueError:
            continue
        raise AssertionError("unsafe candidate accepted")
    print("GLM-5.3 W4A8 H100 standalone argv prototype PASS")
