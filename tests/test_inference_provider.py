import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from drift import inference_provider as provider


class Clock:
    def __init__(self, value=1.0):
        self.value = value

    def __call__(self):
        return self.value


def profile(*, features=frozenset()):
    return provider.ProviderProfile(
        profile_id="test/synthetic-profile",
        model_id="test/synthetic-model",
        availability=provider.Availability.AVAILABLE,
        supported_features=features,
        qualification_id="e" * 64,
    )


def request(*, features=frozenset(), limits=None):
    return provider.InferenceRequest(
        identity=provider.ProviderIdentity("test-provider", "test-instance"),
        profile_id="test/synthetic-profile",
        model_id="test/synthetic-model",
        request_id="a" * 32,
        attempt_id="b" * 32,
        issued_at=1.0,
        deadline=10.0,
        prompt="hello",
        limits=limits or provider.InferenceLimits(100, 20, 8, 100),
        features=features,
    )


def event(req, sequence, kind, **changes):
    return provider.ProviderEvent(
        identity=req.identity,
        profile_id=req.profile_id,
        model_id=req.model_id,
        request_id=req.request_id,
        attempt_id=req.attempt_id,
        sequence=sequence,
        kind=kind,
        **changes,
    )


def error_code(expected, call):
    with pytest.raises(provider.ProviderContractError) as caught:
        call()
    assert caught.value.code is expected


def test_exact_required_profiles_are_unavailable_without_metadata_or_flag_override():
    assert set(provider.REQUIRED_PROFILES) == {
        "deepseek-ai/DeepSeek-V4.1-Flash",
        "zai-org/GLM-5.3",
    }
    assert "zai-org/GLM-5.3-Flash" not in provider.REQUIRED_PROFILES
    for model_id, value in provider.REQUIRED_PROFILES.items():
        assert value == provider.ProviderProfile(model_id, model_id, provider.Availability.UNAVAILABLE)
        assert value.supported_features == frozenset() and value.qualification_id is None
        with pytest.raises(FrozenInstanceError):
            value.availability = provider.Availability.AVAILABLE
        error_code(
            provider.ContractCode.MALFORMED,
            lambda model_id=model_id: provider.ProviderProfile(
                model_id,
                model_id,
                provider.Availability.AVAILABLE,
                qualification_id="f" * 64,
            ),
        )
    with pytest.raises(TypeError):
        provider.REQUIRED_PROFILES["other"] = profile()
    error_code(
        provider.ContractCode.MALFORMED,
        lambda: provider.ProviderProfile(
            "third-party/profile",
            "third-party/model",
            provider.Availability.AVAILABLE,
            qualification_id="f" * 64,
        ),
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: provider.ProviderIdentity("bad id", "instance"),
        lambda: provider.ProviderIdentity("provider", "\ud800"),
        lambda: provider.InferenceLimits(True, 1, 1, 1),
        lambda: provider.InferenceLimits(1, -1, 1, 1),
        lambda: provider.InferenceLimits(1, 1, 0, 1),
        lambda: replace(request(), issued_at=1),
        lambda: replace(request(), deadline=1.0),
        lambda: replace(request(), request_id="A" * 32),
        lambda: replace(request(), features={provider.Feature.TOOLS}),
        lambda: provider.Usage(1, 2, 4),
        lambda: provider.Usage(True, 0, 1),
        lambda: provider.Refusal(provider.RefusalCode.UNSUPPORTED_FEATURE),
        lambda: provider.Refusal(provider.RefusalCode.PROFILE_UNAVAILABLE, (provider.Feature.TOOLS,)),
    ],
)
def test_contracts_reject_malformed_or_unbounded_values(build):
    error_code(provider.ContractCode.MALFORMED, build)


def test_happy_stream_is_exact_bounded_monotonic_and_single_terminal():
    clock, req = Clock(2.0), request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    accepted = stream.accept(event(req, 0, provider.EventKind.STARTED))
    assert accepted.accepted_at == 2.0
    clock.value = 3.0
    stream.accept(event(req, 1, provider.EventKind.OUTPUT, text="answer "))
    clock.value = 4.0
    usage = provider.Usage(2, 3, 5)
    terminal = event(req, 2, provider.EventKind.COMPLETED, usage=usage)
    stream.accept(terminal)
    assert stream.terminal == terminal and stream.terminal_reported and not stream.stop_reported
    assert stream.checkpoint().events[-1].event.usage == usage
    error_code(
        provider.ContractCode.TERMINAL,
        lambda: stream.accept(event(req, 3, provider.EventKind.OUTPUT, text="late")),
    )


@pytest.mark.parametrize("field", ["identity", "profile_id", "model_id", "request_id", "attempt_id"])
def test_every_event_identity_dimension_must_match(field):
    req = request()
    changes = {
        "identity": provider.ProviderIdentity("other", "instance"),
        "profile_id": "test/other-profile",
        "model_id": "test/other-model",
        "request_id": "c" * 32,
        "attempt_id": "d" * 32,
    }
    frame = replace(event(req, 0, provider.EventKind.STARTED), **{field: changes[field]})
    error_code(
        provider.ContractCode.IDENTITY_MISMATCH,
        lambda: provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0)).accept(frame),
    )


def test_replayed_gapped_and_post_terminal_frames_are_rejected():
    req = request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    stream.accept(event(req, 0, provider.EventKind.STARTED))
    error_code(
        provider.ContractCode.SEQUENCE, lambda: stream.accept(event(req, 0, provider.EventKind.OUTPUT, text="x"))
    )
    error_code(
        provider.ContractCode.SEQUENCE, lambda: stream.accept(event(req, 2, provider.EventKind.OUTPUT, text="x"))
    )
    stream.accept(event(req, 1, provider.EventKind.FAILED, failure_code="backend_failed"))
    error_code(
        provider.ContractCode.TERMINAL, lambda: stream.accept(event(req, 2, provider.EventKind.OUTPUT, text="x"))
    )


def test_unavailable_and_unsupported_profiles_require_exact_typed_refusal():
    real = provider.REQUIRED_PROFILES[provider.DEEPSEEK_V41_FLASH]
    req = replace(request(), profile_id=real.profile_id, model_id=real.model_id)
    clock = Clock(2.0)
    stream = provider.ProviderStreamValidator(real, req, clock=clock)
    unavailable = provider.Refusal(provider.RefusalCode.PROFILE_UNAVAILABLE)
    error_code(provider.ContractCode.MALFORMED, lambda: stream.accept(event(req, 0, provider.EventKind.STARTED)))
    stream.accept(event(req, 0, provider.EventKind.REFUSED, refusal=unavailable))

    req = request(features=frozenset({provider.Feature.TOOLS, provider.Feature.LOGPROBS}))
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    refusal = provider.Refusal(
        provider.RefusalCode.UNSUPPORTED_FEATURE,
        (provider.Feature.LOGPROBS, provider.Feature.TOOLS),
    )
    assert provider.refusal_for(profile(), req) == refusal
    stream.accept(event(req, 0, provider.EventKind.REFUSED, refusal=refusal))


def test_cancel_request_is_not_execution_stopped_and_requires_matching_stop():
    clock, req = Clock(2.0), request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    stream.accept(event(req, 0, provider.EventKind.STARTED))
    clock.value = 3.0
    cancellation = stream.request_cancel()
    assert cancellation.after_sequence == 1
    assert stream.cancel_requested and not stream.stop_reported and stream.terminal is None
    assert stream.request_cancel() == cancellation
    error_code(
        provider.ContractCode.MALFORMED,
        lambda: stream.request_cancel(provider.StopReason.RECOVERY),
    )
    clock.value = 4.0
    error_code(
        provider.ContractCode.CANCELLED,
        lambda: stream.accept(event(req, 1, provider.EventKind.OUTPUT, text="after cancel")),
    )
    error_code(
        provider.ContractCode.CANCELLED,
        lambda: stream.accept(event(req, 1, provider.EventKind.STOPPED, stop_reason=provider.StopReason.DEADLINE)),
    )
    stopped = event(req, 1, provider.EventKind.STOPPED, stop_reason=provider.StopReason.CANCELLED)
    stream.accept(stopped)
    assert stream.stop_reported and stream.terminal == stopped


def test_cancel_retry_after_matching_stopped_returns_original_intent():
    clock, req = Clock(2.0), request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    stream.accept(event(req, 0, provider.EventKind.STARTED))
    cancellation = stream.request_cancel()
    stream.accept(event(req, 1, provider.EventKind.STOPPED, stop_reason=provider.StopReason.CANCELLED))

    assert stream.request_cancel() is cancellation


def test_fresh_cancel_after_completed_still_rejects_terminal():
    req = request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    stream.accept(event(req, 0, provider.EventKind.STARTED))
    stream.accept(event(req, 1, provider.EventKind.COMPLETED, usage=provider.Usage(1, 1, 2)))

    error_code(provider.ContractCode.TERMINAL, stream.request_cancel)


def test_absolute_deadline_and_clock_are_local_and_fail_closed():
    clock, req = Clock(2.0), request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    stream.accept(event(req, 0, provider.EventKind.STARTED))
    clock.value = 1.5
    error_code(
        provider.ContractCode.CLOCK,
        lambda: stream.accept(event(req, 1, provider.EventKind.OUTPUT, text="clock reversal")),
    )
    clock.value = req.deadline
    error_code(
        provider.ContractCode.DEADLINE,
        lambda: stream.accept(event(req, 1, provider.EventKind.OUTPUT, text="late")),
    )
    cancellation = stream.enforce_deadline()
    assert cancellation.reason is provider.StopReason.DEADLINE and not stream.stop_reported
    clock.value = req.deadline + 1
    stream.accept(event(req, 1, provider.EventKind.STOPPED, stop_reason=provider.StopReason.DEADLINE))


def test_system_cancel_reasons_cannot_be_minted_by_public_cancel():
    clock, req = Clock(2.0), request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    for reason in (provider.StopReason.DEADLINE, provider.StopReason.RECOVERY):
        error_code(provider.ContractCode.MALFORMED, lambda reason=reason: stream.request_cancel(reason))
        assert stream.checkpoint().cancellation is None
    clock.value = req.deadline
    cancellation = stream.enforce_deadline()
    assert cancellation.reason is provider.StopReason.DEADLINE


def test_usage_output_and_event_limits_reject_without_advancing_sequence():
    limits = provider.InferenceLimits(2, 3, 3, 4)
    clock, req = Clock(2.0), request(limits=limits)
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    stream.accept(event(req, 0, provider.EventKind.STARTED))
    error_code(
        provider.ContractCode.LIMIT,
        lambda: stream.accept(event(req, 1, provider.EventKind.OUTPUT, text="12345")),
    )
    stream.accept(event(req, 1, provider.EventKind.OUTPUT, text="1234"))
    error_code(
        provider.ContractCode.LIMIT,
        lambda: stream.accept(event(req, 2, provider.EventKind.COMPLETED, usage=provider.Usage(2, 4, 6))),
    )
    stream.accept(event(req, 2, provider.EventKind.COMPLETED, usage=provider.Usage(2, 3, 5)))

    one = request(limits=provider.InferenceLimits(1, 1, 1, 1))
    bounded = provider.ProviderStreamValidator(profile(), one, clock=clock)
    error_code(
        provider.ContractCode.LIMIT,
        lambda: bounded.accept(event(one, 0, provider.EventKind.STARTED)),
    )


def test_transitions_and_checkpoints_are_atomic_across_cancel(monkeypatch):
    clock, req = Clock(2.0), request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    original = provider.AcceptedEvent
    constructing = threading.Event()
    release = threading.Event()
    cancel_started = threading.Event()
    cancel_done = threading.Event()
    outcomes = []

    def blocked_accepted(*args, **kwargs):
        constructing.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(provider, "AcceptedEvent", blocked_accepted)

    def accept():
        outcomes.append(("accept", stream.accept(event(req, 0, provider.EventKind.STARTED))))

    def cancel():
        cancel_started.set()
        outcomes.append(("cancel", stream.request_cancel()))
        cancel_done.set()

    accept_thread = threading.Thread(target=accept)
    cancel_thread = threading.Thread(target=cancel)
    accept_thread.start()
    assert constructing.wait(2)
    cancel_thread.start()
    assert cancel_started.wait(2)
    try:
        # The accepted frame owns one indivisible transition through append.
        assert not cancel_done.wait(0.2)
    finally:
        release.set()
        accept_thread.join(2)
        cancel_thread.join(2)
        monkeypatch.setattr(provider, "AcceptedEvent", original)
    assert not accept_thread.is_alive() and not cancel_thread.is_alive()
    assert {kind for kind, _value in outcomes} == {"accept", "cancel"}
    assert stream.checkpoint().cancellation.after_sequence == 1
    recovered = provider.ProviderStreamValidator.recover(profile(), req, stream.checkpoint(), clock=clock)
    recovered.accept(event(req, 1, provider.EventKind.STOPPED, stop_reason=provider.StopReason.CANCELLED))


def test_reentrant_clock_cannot_mutate_the_validator():
    req = request()
    stream = None
    entered = False

    def clock():
        nonlocal entered
        if stream is not None and not entered:
            entered = True
            try:
                stream.request_cancel()
            finally:
                entered = False
        return 2.0

    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    error_code(provider.ContractCode.CLOCK, lambda: stream.accept(event(req, 0, provider.EventKind.STARTED)))
    assert stream.checkpoint() == provider.StreamCheckpoint((), None)


def _fill_nonterminal_prefix(stream, req, count):
    if count:
        stream.accept(event(req, 0, provider.EventKind.STARTED))
    for sequence in range(1, count):
        stream.accept(event(req, sequence, provider.EventKind.OUTPUT, text="x"))


def test_one_event_limit_reserves_the_only_slot_for_a_terminal():
    limits = provider.InferenceLimits(1, 1, 1, 1)
    req = request(limits=limits)
    stream = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    error_code(provider.ContractCode.LIMIT, lambda: stream.accept(event(req, 0, provider.EventKind.STARTED)))
    stream.accept(event(req, 0, provider.EventKind.FAILED, failure_code="bounded"))

    stopped = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    stopped.request_cancel()
    stopped.accept(event(req, 0, provider.EventKind.STOPPED, stop_reason=provider.StopReason.CANCELLED))

    unavailable = provider.REQUIRED_PROFILES[provider.DEEPSEEK_V41_FLASH]
    refused_request = replace(req, profile_id=unavailable.profile_id, model_id=unavailable.model_id)
    refused = provider.ProviderStreamValidator(unavailable, refused_request, clock=Clock(2.0))
    refused.accept(
        event(
            refused_request,
            0,
            provider.EventKind.REFUSED,
            refusal=provider.Refusal(provider.RefusalCode.PROFILE_UNAVAILABLE),
        )
    )


@pytest.mark.parametrize("max_events", [2, provider.MAX_EVENTS])
def test_total_event_limit_always_retains_normal_failure_and_stop_terminals(max_events):
    limits = provider.InferenceLimits(10, 10, max_events, provider.MAX_EVENT_BYTES)
    req = request(limits=limits)
    prefix = max_events - 1

    completed = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    _fill_nonterminal_prefix(completed, req, prefix)
    completed.accept(event(req, prefix, provider.EventKind.COMPLETED, usage=provider.Usage(0, 0, 0)))

    failed = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    _fill_nonterminal_prefix(failed, req, prefix)
    failed.accept(event(req, prefix, provider.EventKind.FAILED, failure_code="bounded"))

    stopped = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    _fill_nonterminal_prefix(stopped, req, prefix)
    cancellation = stopped.request_cancel()
    assert cancellation.after_sequence == prefix
    stopped.accept(event(req, prefix, provider.EventKind.STOPPED, stop_reason=provider.StopReason.CANCELLED))


def test_rejected_last_nonterminal_does_not_consume_the_terminal_slot():
    req = request(limits=provider.InferenceLimits(1, 1, 2, 1))
    stream = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    stream.accept(event(req, 0, provider.EventKind.STARTED))
    error_code(
        provider.ContractCode.LIMIT,
        lambda: stream.accept(event(req, 1, provider.EventKind.OUTPUT, text="x")),
    )
    stream.accept(event(req, 1, provider.EventKind.COMPLETED, usage=provider.Usage(0, 0, 0)))


def test_completed_recovery_preserves_terminal_without_cancel_or_new_execution():
    clock, req = Clock(2.0), request()
    original = provider.ProviderStreamValidator(profile(), req, clock=clock)
    original.accept(event(req, 0, provider.EventKind.STARTED))
    clock.value = 3.0
    completed = event(req, 1, provider.EventKind.COMPLETED, usage=provider.Usage(1, 1, 2))
    original.accept(completed)
    clock.value = 20.0
    recovered = provider.ProviderStreamValidator.recover(profile(), req, original.checkpoint(), clock=clock)
    assert recovered.terminal == completed and recovered.terminal_reported and not recovered.stop_reported
    assert not recovered.cancel_requested
    error_code(
        provider.ContractCode.TERMINAL,
        lambda: recovered.accept(event(req, 2, provider.EventKind.STARTED)),
    )


def test_incomplete_recovery_requests_stop_without_inventing_terminal_or_usage():
    clock, req = Clock(2.0), request()
    original = provider.ProviderStreamValidator(profile(), req, clock=clock)
    original.accept(event(req, 0, provider.EventKind.STARTED))
    original.accept(event(req, 1, provider.EventKind.OUTPUT, text="partial"))
    clock.value = 5.0
    recovered = provider.ProviderStreamValidator.recover(profile(), req, original.checkpoint(), clock=clock)
    assert recovered.cancel_requested and not recovered.stop_reported and recovered.terminal is None
    assert recovered.checkpoint().cancellation.reason is provider.StopReason.RECOVERY
    clock.value = 6.0
    recovered.accept(event(req, 2, provider.EventKind.STOPPED, stop_reason=provider.StopReason.RECOVERY))
    assert recovered.stop_reported


def test_overdue_recovery_uses_deadline_but_preserves_prior_cancel():
    req = request()
    original = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    original.accept(event(req, 0, provider.EventKind.STARTED))
    overdue = provider.ProviderStreamValidator.recover(profile(), req, original.checkpoint(), clock=Clock(20.0))
    assert overdue.checkpoint().cancellation.reason is provider.StopReason.DEADLINE

    cancelled = provider.ProviderStreamValidator(profile(), req, clock=Clock(2.0))
    cancelled.accept(event(req, 0, provider.EventKind.STARTED))
    prior = cancelled.request_cancel()
    restored = provider.ProviderStreamValidator.recover(profile(), req, cancelled.checkpoint(), clock=Clock(20.0))
    assert restored.checkpoint().cancellation == prior


def test_prompt_and_output_are_suppressed_from_nested_representations():
    secret = "synthetic-secret-value"
    req = replace(request(), prompt=secret)
    frame = event(req, 0, provider.EventKind.OUTPUT, text=secret)
    accepted = provider.AcceptedEvent(frame, 2.0)
    checkpoint = provider.StreamCheckpoint((accepted,), None)
    assert all(secret not in repr(value) for value in (req, frame, accepted, checkpoint))


def test_recovery_preserves_existing_cancel_and_stopped_terminal_exactly():
    clock, req = Clock(2.0), request()
    original = provider.ProviderStreamValidator(profile(), req, clock=clock)
    original.accept(event(req, 0, provider.EventKind.STARTED))
    clock.value = 3.0
    cancellation = original.request_cancel()
    clock.value = 4.0
    stopped = event(req, 1, provider.EventKind.STOPPED, stop_reason=provider.StopReason.CANCELLED)
    original.accept(stopped)

    clock.value = 20.0
    recovered = provider.ProviderStreamValidator.recover(profile(), req, original.checkpoint(), clock=clock)
    assert recovered.checkpoint().cancellation == cancellation
    assert recovered.terminal == stopped and recovered.stop_reported


def test_recovery_rejects_changed_identity_sequence_and_clock_evidence():
    clock, req = Clock(2.0), request()
    stream = provider.ProviderStreamValidator(profile(), req, clock=clock)
    first = stream.accept(event(req, 0, provider.EventKind.STARTED))
    wrong = replace(first, event=replace(first.event, attempt_id="c" * 32))
    error_code(
        provider.ContractCode.IDENTITY_MISMATCH,
        lambda: provider.ProviderStreamValidator.recover(
            profile(), req, provider.StreamCheckpoint((wrong,), None), clock=clock
        ),
    )
    gap = replace(first, event=replace(first.event, sequence=1))
    error_code(
        provider.ContractCode.SEQUENCE,
        lambda: provider.ProviderStreamValidator.recover(
            profile(), req, provider.StreamCheckpoint((gap,), None), clock=clock
        ),
    )
    before_issue = replace(first, accepted_at=0.5)
    error_code(
        provider.ContractCode.CLOCK,
        lambda: provider.ProviderStreamValidator.recover(
            profile(), req, provider.StreamCheckpoint((before_issue,), None), clock=clock
        ),
    )
    early_cancel = provider.CancellationRequest(
        req.identity,
        req.request_id,
        req.attempt_id,
        provider.StopReason.CANCELLED,
        1.5,
        1,
    )
    error_code(
        provider.ContractCode.CLOCK,
        lambda: provider.ProviderStreamValidator.recover(
            profile(), req, provider.StreamCheckpoint((first,), early_cancel), clock=clock
        ),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": provider.EventKind.OUTPUT},
        {"kind": provider.EventKind.STARTED, "text": "unexpected"},
        {"kind": provider.EventKind.COMPLETED},
        {"kind": provider.EventKind.FAILED, "failure_code": "BAD-CODE"},
        {"kind": provider.EventKind.STOPPED},
    ],
)
def test_malformed_event_field_combinations_are_rejected(changes):
    req = request()
    values = dict(
        identity=req.identity,
        profile_id=req.profile_id,
        model_id=req.model_id,
        request_id=req.request_id,
        attempt_id=req.attempt_id,
        sequence=0,
        kind=provider.EventKind.STARTED,
    )
    values.update(changes)
    error_code(provider.ContractCode.MALFORMED, lambda: provider.ProviderEvent(**values))
