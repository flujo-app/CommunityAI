import pytest

from drift.node.route_metrics import RouteOutcomeTracker, aggregate_route_observations, validate_route_observation

DIGEST = "sha256:" + "a" * 64


def test_tracker_exposes_only_quantized_content_free_aggregates():
    now = [1_000.0]
    tracker = RouteOutcomeTracker(clock=lambda: now[0])

    tracker.record(
        manifest_digest=DIGEST,
        succeeded=True,
        completion_tokens=8,
        duration_seconds=2,
    )
    tracker.record(
        manifest_digest=DIGEST,
        succeeded=False,
        completion_tokens=999,
        duration_seconds=1,
    )
    observation = tracker.snapshot(DIGEST)

    assert observation == {
        "schema_version": 1,
        "manifest_digest": DIGEST,
        "window_seconds": 300,
        "attempts_bucket": 2,
        "successes_bucket": 1,
        "useful_tokens_per_second_milli": 4_000,
        "reliability_milli": 500,
        "age_seconds_bucket": 0,
    }
    assert not {
        "prompt",
        "output",
        "text",
        "tokens",
        "api_key",
        "request_id",
        "ip",
        "path",
        "error",
        "duration_seconds",
    }.intersection(observation)


def test_tracker_bounds_memory_counts_and_expires_the_window():
    now = [2_000.0]
    tracker = RouteOutcomeTracker(maximum_events_per_model=64, clock=lambda: now[0])
    for _ in range(100):
        tracker.record(
            manifest_digest=DIGEST,
            succeeded=True,
            completion_tokens=1,
            duration_seconds=1,
        )

    observation = tracker.snapshot(DIGEST)
    assert observation["attempts_bucket"] == 64
    assert observation["successes_bucket"] == 64

    now[0] += 301
    assert tracker.snapshot(DIGEST) is None


def test_tracker_rolls_to_a_new_aggregate_without_retaining_events():
    now = [299.0]
    tracker = RouteOutcomeTracker(clock=lambda: now[0])
    tracker.record(
        manifest_digest=DIGEST,
        succeeded=True,
        completion_tokens=8,
        duration_seconds=2,
    )

    now[0] = 301.0
    tracker.record(
        manifest_digest=DIGEST,
        succeeded=False,
        completion_tokens=0,
        duration_seconds=1,
    )

    assert tracker.snapshot(DIGEST)["attempts_bucket"] == 1
    assert tracker.snapshot(DIGEST)["successes_bucket"] == 0
    windows = tracker._windows[DIGEST]
    assert len(windows) == 2
    assert all(not hasattr(window, "events") for window in windows)


def test_tracker_age_quantization_never_extends_freshness():
    now = [1_000.0]
    tracker = RouteOutcomeTracker(clock=lambda: now[0])
    tracker.record(
        manifest_digest=DIGEST,
        succeeded=True,
        completion_tokens=1,
        duration_seconds=1,
    )
    now[0] += 91

    observation = tracker.snapshot(DIGEST)

    assert observation["age_seconds_bucket"] == 105
    with pytest.raises(ValueError, match="stale"):
        validate_route_observation(
            observation,
            expected_manifest_digest=DIGEST,
            maximum_age_seconds=90,
        )


def test_route_observation_validation_is_exact_digest_bound_and_fresh():
    source = {
        "schema_version": 1,
        "manifest_digest": DIGEST,
        "window_seconds": 300,
        "attempts_bucket": 4,
        "successes_bucket": 2,
        "useful_tokens_per_second_milli": 2_000,
        "reliability_milli": 500,
        "age_seconds_bucket": 90,
    }
    parsed = validate_route_observation(
        source,
        expected_manifest_digest=DIGEST,
        maximum_age_seconds=90,
    )
    assert parsed.attempts_bucket == 4

    with pytest.raises(ValueError, match="different manifest"):
        validate_route_observation(
            source,
            expected_manifest_digest="sha256:" + "b" * 64,
            maximum_age_seconds=90,
        )

    stale = dict(source, age_seconds_bucket=105)
    with pytest.raises(ValueError, match="stale"):
        validate_route_observation(
            stale,
            expected_manifest_digest=DIGEST,
            maximum_age_seconds=90,
        )

    private = dict(source, private_path="forbidden")
    with pytest.raises(ValueError, match="unknown fields"):
        validate_route_observation(
            private,
            expected_manifest_digest=DIGEST,
            maximum_age_seconds=90,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"manifest_digest": "alias", "succeeded": True, "completion_tokens": 1, "duration_seconds": 1},
        {"manifest_digest": DIGEST, "succeeded": 1, "completion_tokens": 1, "duration_seconds": 1},
        {"manifest_digest": DIGEST, "succeeded": True, "completion_tokens": -1, "duration_seconds": 1},
        {"manifest_digest": DIGEST, "succeeded": True, "completion_tokens": 1, "duration_seconds": 0},
    ],
)
def test_tracker_rejects_unbounded_or_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        RouteOutcomeTracker().record(**kwargs)


def test_only_closed_thresholded_windows_are_publishable():
    now = [299.0]
    tracker = RouteOutcomeTracker(clock=lambda: now[0])
    for _ in range(4):
        tracker.record(
            manifest_digest=DIGEST,
            succeeded=True,
            completion_tokens=2,
            duration_seconds=1,
        )

    assert tracker.closed_snapshot(DIGEST) is None
    now[0] = 301.0
    closed = tracker.closed_snapshot(DIGEST)
    assert closed["attempts_bucket"] == 4
    assert closed["successes_bucket"] == 4

    sparse = RouteOutcomeTracker(clock=lambda: now[0])
    now[0] = 599.0
    for _ in range(3):
        sparse.record(manifest_digest=DIGEST, succeeded=True, completion_tokens=1, duration_seconds=1)
    now[0] = 601.0
    assert sparse.closed_snapshot(DIGEST) is None


def test_remote_aggregate_requires_two_valid_signers_and_uses_bounded_medians():
    first = {
        "schema_version": 1,
        "manifest_digest": DIGEST,
        "window_seconds": 300,
        "attempts_bucket": 4,
        "successes_bucket": 2,
        "useful_tokens_per_second_milli": 2_000,
        "reliability_milli": 500,
        "age_seconds_bucket": 15,
    }
    second = dict(
        first,
        attempts_bucket=16,
        successes_bucket=8,
        useful_tokens_per_second_milli=8_000,
        reliability_milli=800,
        age_seconds_bucket=30,
    )
    malformed = dict(first, private_request_id="forbidden")

    assert (
        aggregate_route_observations(
            (first,),
            expected_manifest_digest=DIGEST,
            maximum_age_seconds=90,
        )
        is None
    )
    aggregate = aggregate_route_observations(
        (malformed, first, second),
        expected_manifest_digest=DIGEST,
        maximum_age_seconds=90,
    )
    assert aggregate == dict(first, age_seconds_bucket=30)
