"""Standalone arithmetic before implementing exact-model capacity admission.

Weight byte counts come from the pinned official model repository trees; they
are lower bounds, not runtime memory or proof that a backend can serve a model.
"""

DEEPSEEK_WEIGHTS = 510_296_708_312
GLM_WEIGHTS = 755_632_050_320
H100_EIGHT_CARD_BYTES = 8 * 80_000_000_000


def shortfall(weights, gpu_bytes):
    if type(weights) is not int or weights <= 0 or type(gpu_bytes) is not tuple:
        raise ValueError("invalid capacity input")
    if not gpu_bytes or any(type(amount) is not int or amount <= 0 for amount in gpu_bytes):
        raise ValueError("invalid GPU capacity")
    return max(0, weights - sum(gpu_bytes))


def main():
    cards = (80_000_000_000,) * 8
    assert sum(cards) == H100_EIGHT_CARD_BYTES
    assert shortfall(DEEPSEEK_WEIGHTS, cards) == 0
    assert shortfall(GLM_WEIGHTS, cards) == 115_632_050_320
    assert shortfall(GLM_WEIGHTS, cards[:4]) == 435_632_050_320
    print("exact-model capacity prototype: GLM GPU-only shortfall on eight 80 GB cards PASS")


if __name__ == "__main__":
    main()
