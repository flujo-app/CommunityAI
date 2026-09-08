"""Frozen entry point for the standalone CommunityAI node sidecar.

The sidecar normally runs ``drift node``. A frozen node also reuses this
executable for supervised contribution workers and first-install catalog
bootstrap, so the explicit modes below replace their ``python -m drift.cli``
forms inside a packaged installation.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import sys
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path


def _runtime_contract() -> dict[str, object]:
    """Import every critical packaged runtime and locate Hivemind's daemon."""
    import fastapi
    import hivemind
    import keyring
    import torch
    import transformers
    import uvicorn

    import drift
    from drift.node.catalog_bootstrap import CATALOG_BOOTSTRAP_SCHEMA_VERSION

    daemon_name = "p2pd.exe" if os.name == "nt" else "p2pd"
    daemon = files("hivemind.hivemind_cli").joinpath(daemon_name)
    if not daemon.is_file():
        raise RuntimeError(f"packaged Hivemind daemon is missing: {daemon_name}")
    return {
        "schema_version": 1,
        "application": "CommunityAI-Node",
        "drift": drift.__version__,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "hivemind": hivemind.__version__,
        "fastapi": fastapi.__version__,
        "uvicorn": uvicorn.__version__,
        "keyring": version("keyring"),
        "p2pd": daemon_name,
        "catalog_bootstrap_schema": CATALOG_BOOTSTRAP_SCHEMA_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _worker_runtime_contract() -> dict[str, object]:
    """Exercise the frozen worker entry point without loading weights or joining a network."""
    from drift.cli.run_server import build_parser
    from drift.server.admission import AdmissionPolicy
    from drift.server.server import Server
    from drift.utils.process_lifetime import tie_child_processes_to_this_process

    parser = build_parser()
    parsed = vars(parser.parse_args(["qualification/model", "--new_swarm", "--throughput", "dry_run"]))
    policy = AdmissionPolicy()
    if parsed["model"] != "qualification/model" or parsed["new_swarm"] is not True:
        raise RuntimeError("packaged worker parser did not retain the bounded self-test contract")
    if (
        parsed["throughput"] != "dry_run"
        or parsed["allow_training_rpcs"] is not False
        or policy.allow_training_rpcs is not False
    ):
        raise RuntimeError("packaged worker defaults are not bounded")
    if not tie_child_processes_to_this_process():
        raise RuntimeError("packaged worker process-lifetime guard could not be armed")
    return {
        "schema_version": 1,
        "application": "CommunityAI-Worker",
        "entrypoint": "server",
        "server_class": Server.__name__,
        "model_loading_performed": False,
        "network_join_performed": False,
        "throughput_mode": parsed["throughput"],
        "training_rpcs_enabled": parsed["allow_training_rpcs"],
        "process_lifetime_guard_armed": True,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _verify_frozen_module_path(module) -> None:
    if getattr(sys, "frozen", False):
        try:
            Path(module.__file__).resolve().relative_to(Path(sys._MEIPASS).resolve(strict=True))
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("native self-test imported a module outside the frozen runtime") from exc


def _bitsandbytes_native_path(cextension) -> str:
    """Require the actual loaded CUDA library, not merely a successful Python import."""
    _verify_frozen_module_path(cextension)
    native = cextension.lib
    if native is None or getattr(native, "compiled_with_cuda", False) is not True:
        raise RuntimeError("bitsandbytes did not load a native CUDA backend")
    name = "libbitsandbytes_cuda124" + (".dll" if os.name == "nt" else ".so")
    try:
        loaded = Path(native._lib._name).resolve(strict=True)
        expected = Path(cextension.__file__).resolve().with_name(name)
        if loaded != expected or not loaded.is_file():
            raise RuntimeError("bitsandbytes loaded an unexpected native library")
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError("bitsandbytes native library path could not be verified") from exc
    return name


def _native_runtime_contract(*, require_cuda: bool = False) -> dict[str, object]:
    """Finite native math with tiny tensors; never fetch weights or join a network.

    Run this dedicated process under the qualification runner's timeout. The CPU
    mode does not initialize CUDA. Required-CUDA mode also exercises lazy linalg
    loading and the pruned bitsandbytes profile, and fails if no GPU is available.
    """
    import torch

    _verify_frozen_module_path(torch)
    if torch.__version__ != "2.6.0+cu124" or torch.version.cuda != "12.4":
        raise RuntimeError("native self-test requires the pinned torch 2.6.0+cu124 runtime")
    torch.set_num_threads(1)
    result = {
        "schema_version": 1,
        "application": "CommunityAI-Native-Runtime",
        "frozen": bool(getattr(sys, "frozen", False)),
        "torch": torch.__version__,
        "cuda_build": torch.version.cuda,
        "model_loading_performed": False,
        "network_join_performed": False,
        "cuda_required": require_cuda,
        "cuda_test_performed": False,
    }
    with torch.inference_mode():
        matrix = torch.tensor([[1.0, 2.0], [3.0, 4.0]], dtype=torch.float32, device="cpu")
        expected = torch.tensor([[5.0, 11.0], [11.0, 25.0]], dtype=torch.float32, device="cpu")
        if not torch.allclose(matrix @ matrix.T, expected, rtol=0, atol=0):
            raise RuntimeError("native CPU matrix multiplication returned an incorrect result")
        result["cpu_matmul_passed"] = True
        if not require_cuda:
            return result
        if not torch.cuda.is_available():
            raise RuntimeError("native self-test requires an available CUDA GPU")
        device = "cuda:0"
        cuda_matrix = matrix.to(device)
        if not torch.allclose(cuda_matrix @ cuda_matrix.T, expected.to(device), rtol=1e-5, atol=1e-5):
            raise RuntimeError("native CUDA matrix multiplication returned an incorrect result")
        diagonal = torch.diag(torch.tensor([1.0, 2.0, 3.0, 4.0], dtype=torch.float32, device=device))
        left, singular, right = torch.linalg.svd(diagonal, full_matrices=False)
        if not torch.allclose(left @ torch.diag(singular) @ right, diagonal, rtol=1e-4, atol=1e-4):
            raise RuntimeError("native CUDA linalg reconstruction returned an incorrect result")

        from bitsandbytes import cextension, functional

        native_name = _bitsandbytes_native_path(cextension)
        _verify_frozen_module_path(functional)
        values = torch.linspace(-1.0, 1.0, 64, dtype=torch.float16, device=device)
        packed, state = functional.quantize_4bit(values, blocksize=64, quant_type="nf4")
        restored = functional.dequantize_4bit(packed, quant_state=state, blocksize=64, quant_type="nf4")
        if restored.shape != values.shape or restored.device != values.device:
            raise RuntimeError("bitsandbytes NF4 roundtrip changed tensor shape or device")
        maximum_error = float((restored - values).abs().max().item())
        if not math.isfinite(maximum_error) or maximum_error > 0.2:
            raise RuntimeError("bitsandbytes NF4 roundtrip returned an incorrect result")
        torch.cuda.synchronize()
        result.update(
            {
                "cuda_test_performed": True,
                "cuda_matmul_passed": True,
                "cuda_linalg_passed": True,
                "bitsandbytes_native_library": native_name,
                "bitsandbytes_nf4_roundtrip_passed": True,
                "bitsandbytes_nf4_maximum_absolute_error": maximum_error,
            }
        )
    return result


def main() -> int:
    # PyInstaller's multiprocessing children must be intercepted before importing
    # Torch, Hivemind, or any application modules.
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if argv == ["--self-test"]:
        print(json.dumps(_runtime_contract(), sort_keys=True))
        return 0
    if argv == ["server", "--self-test"]:
        print(json.dumps(_worker_runtime_contract(), sort_keys=True))
        return 0
    if argv in (["--native-self-test"], ["--native-self-test", "--require-cuda"]):
        print(json.dumps(_native_runtime_contract(require_cuda="--require-cuda" in argv), sort_keys=True))
        return 0
    if "--native-self-test" in argv or "--require-cuda" in argv:
        raise RuntimeError("Use --native-self-test with only the optional --require-cuda flag")
    if argv[:1] == ["server"]:
        sys.argv = ["CommunityAI-Node server", *argv[1:]]
        from drift.cli.run_server import main as run
    elif argv[:1] == ["edge-acquire"]:
        sys.argv = ["CommunityAI-Node edge-acquire", *argv[1:]]
        from drift.cli.run_edge_acquisition import main as run
    elif argv[:1] == ["bootstrap"]:
        sys.argv = ["CommunityAI-Node bootstrap", *argv[1:]]
        from drift.cli.run_bootstrap import main as run
    else:
        from drift.cli.run_node import main as run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
