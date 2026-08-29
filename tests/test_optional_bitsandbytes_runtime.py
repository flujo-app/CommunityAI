import os
import subprocess
import sys
from pathlib import Path


def test_peft_tolerates_an_unusable_optional_bitsandbytes_runtime(tmp_path: Path):
    fake_package = tmp_path / "bitsandbytes"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text("raise RuntimeError('native runtime unavailable')\n", encoding="utf-8")
    repository_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(repository_root / "src"), env.get("PYTHONPATH", "")])

    subprocess.check_call(
        [sys.executable, "-c", "from drift.utils import peft; assert peft.bnb is None"],
        env=env,
    )
