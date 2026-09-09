import pytest

from drift.utils.hub_ranges import RANGE_BYTES
from scripts.validate_manifest_resume import validate_resume_observations


def test_accepts_exact_parallel_bounded_resume_ranges_in_response_order():
    size = RANGE_BYTES * 3
    observations = [
        {"requested_range": f"bytes={start}-{end}", "status": 206, "content_range": f"bytes {start}-{end}/{size}"}
        for start, end in [
            (RANGE_BYTES + 100, RANGE_BYTES * 2 + 99),
            (100, RANGE_BYTES + 99),
            (RANGE_BYTES * 2 + 100, size - 1),
        ]
    ]
    validate_resume_observations(observations, prefix_size=100, artifact_size=size)
    for altered in (
        observations[:-1],
        observations + observations[:1],
        [{**item, "status": 200} for item in observations],
    ):
        with pytest.raises(RuntimeError):
            validate_resume_observations(altered, prefix_size=100, artifact_size=size)


def test_small_artifact_requires_exact_open_ended_resume():
    valid = [{"requested_range": "bytes=100-", "status": 206, "content_range": "bytes 100-199/200"}]
    validate_resume_observations(valid, prefix_size=100, artifact_size=200)
    with pytest.raises(RuntimeError):
        validate_resume_observations(
            [{**valid[0], "content_range": "bytes 99-199/200"}], prefix_size=100, artifact_size=200
        )
