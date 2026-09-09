import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_route_setup_pins_configured_runtime_and_preserves_the_route_helpers():
    source = (ROOT / "scripts" / "gate13_route_setup.sh").read_text(encoding="utf-8")
    config = json.loads((ROOT / "config" / "gate13_gcp.json").read_text(encoding="utf-8"))

    assert f'test "$(stat -c %s "$wheel")" = "{config["route_wheel_bytes"]}"' in source
    assert config["route_wheel_sha256"] in source
    assert "fc385f74e02ca955203b1fc5e8ae493c7f4ccd31bd7383c2ae0a1c461c91363e" in source
    assert "bdcc9f499a7cd6b727c0e33a0c4c2b0e71e76e28f3f21cb99804a8f39edfa0d2" in source
    assert "metadata.google.internal" in source
    assert "communityai-qwen.service" in source
    assert "communityai-gemma.service" in source
    assert 'rm -rf "$root"' in source
