"""Standalone capacity arithmetic for a pinned GLM-5.3 quantized candidate."""

OFFICIAL_FP8_BYTES = 755_632_050_320
W4A8_BYTES = 399_716_726_536
EIGHT_H100_DECIMAL_BYTES = 8 * 80_000_000_000


if __name__ == "__main__":
    assert OFFICIAL_FP8_BYTES - EIGHT_H100_DECIMAL_BYTES == 115_632_050_320
    assert W4A8_BYTES < EIGHT_H100_DECIMAL_BYTES
    assert EIGHT_H100_DECIMAL_BYTES - W4A8_BYTES == 240_283_273_464
    print("GLM W4A8 pinned artifact-byte prototype PASS")
