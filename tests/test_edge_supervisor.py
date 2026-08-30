import json
import sys
import textwrap

import psutil

import drift.node.edge_supervisor as edge_supervisor
from drift.node.edge_supervisor import supervise_edge_benchmark


def _child_result():
    return {
        "schema_version": 2,
        "measured_at_unix": 1,
        "runtime": {"python": "test", "platform": "test", "drift": "test", "torch": "test"},
        "model": {
            "id": "test",
            "manifest_digest": "sha256:" + "a" * 64,
            "repository": "org/test",
            "revision": "b" * 40,
            "dtype": "float32",
        },
        "workload": {
            "prompt": "private prompt",
            "prompt_tokens": 2,
            "requested_new_tokens": 2,
            "generated_tokens": 2,
            "output_ids": [1, 2],
            "decoded": "private output",
        },
        "storage": {
            "cold_start": False,
            "cache_bytes_before": 10,
            "cache_bytes_after": 10,
            "cache_growth_bytes": 0,
        },
        "memory": {
            "process_tree_rss_baseline_bytes": 100,
            "process_tree_rss_loaded_bytes": 200,
            "process_tree_rss_post_close_bytes": 300,
            "process_tree_rss_post_close_delta_bytes": 200,
            "process_tree_rss_peak_bytes": 400,
            "process_tree_rss_peak_delta_bytes": 300,
            "accelerators": {},
            "client_components": {"components": {}, "unique_parameter_bytes": 0},
        },
        "latency": {
            "load_seconds": 1,
            "first_token_seconds": 1,
            "total_generation_seconds": 2,
            "decode_seconds_after_first_token": 1,
            "decode_tokens_per_second": 1,
        },
        "cleanup": {
            "runtime_close": {"clean": True},
            "route_manager": {"observed": True, "clean": True},
            "process_tree": {"clean": True},
            "memory": {"clean": False},
            "accelerators": {"clean": True},
            "passed": False,
        },
    }


def _write_helper(path, result, *, spawn_survivor=False):
    survivor = "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])" if spawn_survivor else ""
    path.write_text(
        textwrap.dedent(
            f"""
            import json
            import pathlib
            import subprocess
            import sys
            import time

            spec = json.load(sys.stdin)
            {survivor}
            time.sleep(0.15)
            pathlib.Path(spec["result_path"]).write_text(
                json.dumps({result!r}), encoding="utf-8"
            )
            """
        ),
        encoding="utf-8",
    )


def _write_late_descendant_helper(path, result):
    path.write_text(
        textwrap.dedent(
            f"""
            import json
            import pathlib
            import subprocess
            import sys
            import time

            spec = json.load(sys.stdin)
            release_path = pathlib.Path(spec["release_path"])
            deadline = time.monotonic() + 5
            while not release_path.is_file():
                if time.monotonic() >= deadline:
                    raise RuntimeError("test release was not observed")
                time.sleep(0.001)
            survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            pathlib.Path(spec["survivor_pid_path"]).write_text(str(survivor.pid), encoding="utf-8")
            pathlib.Path(spec["result_path"]).write_text(
                json.dumps({result!r}), encoding="utf-8"
            )
            """
        ),
        encoding="utf-8",
    )


def test_supervisor_emits_schema_v3_and_uses_process_exit_for_memory_cleanup(tmp_path):
    helper = tmp_path / "clean_child.py"
    _write_helper(helper, _child_result())

    result = supervise_edge_benchmark(
        {"private_value": "secret"},
        timeout_seconds=5,
        sample_interval_seconds=0.01,
        exit_grace_seconds=0.1,
        _child_command=[sys.executable, str(helper)],
    )

    assert result["schema_version"] == 3
    assert result["workload"]["prompt_retained"] is False
    assert result["workload"]["output_retained"] is False
    assert "prompt" not in result["workload"]
    assert "output_ids" not in result["workload"]
    assert "decoded" not in result["workload"]
    assert result["cleanup"]["memory"]["clean"] is False
    assert result["cleanup"]["memory"]["diagnostic_only"] is True
    assert result["cleanup"]["supervisor"]["clean"] is True
    assert result["cleanup"]["supervisor"]["forced_cleanup_required"] is False
    assert result["cleanup"]["supervisor"]["forced_cleanup_succeeded"] is True
    assert result["cleanup"]["supervisor"]["all_contained_processes_absent"] is True
    assert result["cleanup"]["supervisor"]["all_tracked_processes_absent"] is True
    assert result["cleanup"]["passed"] is True
    assert result["memory"]["process_tree_rss_peak_bytes"] > 0

    serialized = json.dumps(result)
    assert "private prompt" not in serialized
    assert "private output" not in serialized
    assert "secret" not in serialized
    assert str(tmp_path) not in serialized


def test_supervisor_fails_cleanup_and_reaps_a_surviving_descendant(tmp_path):
    helper = tmp_path / "surviving_child.py"
    _write_helper(helper, _child_result(), spawn_survivor=True)

    result = supervise_edge_benchmark(
        {},
        timeout_seconds=5,
        sample_interval_seconds=0.01,
        exit_grace_seconds=0.05,
        _child_command=[sys.executable, str(helper)],
    )

    assert result["cleanup"]["supervisor"]["forced_cleanup_required"] is True
    assert result["cleanup"]["supervisor"]["descendants_exited"] is False
    assert result["cleanup"]["supervisor"]["forced_cleanup_succeeded"] is True
    assert result["cleanup"]["supervisor"]["all_contained_processes_absent"] is True
    assert result["cleanup"]["supervisor"]["all_tracked_processes_absent"] is True
    assert result["cleanup"]["supervisor"]["clean"] is False
    assert result["cleanup"]["passed"] is False


def test_supervisor_contains_descendant_spawned_after_last_tree_sample(tmp_path, monkeypatch):
    helper = tmp_path / "late_descendant_child.py"
    release_path = tmp_path / "release"
    survivor_pid_path = tmp_path / "survivor.pid"
    _write_late_descendant_helper(helper, _child_result())

    original_sample_tree = edge_supervisor._sample_tree
    samples = 0

    def release_after_first_sample(psutil_module, pid):
        nonlocal samples
        samples += 1
        if samples == 1:
            measurement = original_sample_tree(psutil_module, pid)
            release_path.write_text("go", encoding="utf-8")
            return measurement
        return 0, 0  # Reproduce a dead-root final sample that cannot discover its late descendant.

    monkeypatch.setattr(edge_supervisor, "_sample_tree", release_after_first_sample)
    result = supervise_edge_benchmark(
        {"release_path": str(release_path), "survivor_pid_path": str(survivor_pid_path)},
        timeout_seconds=5,
        sample_interval_seconds=1,
        exit_grace_seconds=0.05,
        _child_command=[sys.executable, str(helper)],
    )

    survivor_pid = int(survivor_pid_path.read_text(encoding="utf-8"))
    assert samples == 2  # One live-root sample and one final dead-root sample; the descendant was never sampled.
    assert not psutil.pid_exists(survivor_pid)
    assert result["cleanup"]["supervisor"]["forced_cleanup_required"] is True
    assert result["cleanup"]["supervisor"]["descendants_exited"] is False
    assert result["cleanup"]["supervisor"]["forced_cleanup_succeeded"] is True
    assert result["cleanup"]["supervisor"]["all_contained_processes_absent"] is True
    assert result["cleanup"]["supervisor"]["clean"] is False
    assert result["cleanup"]["passed"] is False
