"""Offline policy tests. Synthetic appraisals are not real attestation evidence."""

import sys
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from confidential_compute_admission import AdmissionDenied, AdmissionGate, Appraisal, Binding, ProviderProfile

D = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def binding(**changes):
    value = Binding(
        "synthetic",
        "factory1",
        "ns",
        "user1",
        "job1",
        "session1",
        "gpu-model",
        "https://worker.example/v1",
        "1" * 64,
        1,
        D,
        D,
        D,
        D,
        D,
        D,
        "account1",
        "developer",
        D,
        "commitment1",
        "reservation1",
        1,
        150.0,
        "synthetic-gpu-v1",
        D,
    )
    return replace(value, **changes)


class SyntheticVerifier:
    def __init__(self):
        self.transform = lambda result: result
        self.calls = 0

    def appraise(self, request):
        self.calls += 1
        result = Appraisal(request, "test-verifier", D, 100.0, 130.0, 140.0, 1, "sev-snp", "nvidia-cc", D)
        return self.transform(result)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.verifier = SyntheticVerifier()
        self.now = 110.0
        self.revision = 1
        profile = ProviderProfile(
            "synthetic-gpu-v1",
            "synthetic",
            D,
            "test-verifier",
            frozenset(("https://worker.example/v1",)),
            frozenset(("cpu-tools", "gpu-model")),
            60,
            30,
            self.verifier,
        )
        self.gate = AdmissionGate((profile,), authority_revision=lambda: self.revision, clock=lambda: self.now)

    def denied(self, code, action):
        with self.assertRaises(AdmissionDenied) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_modal_denied_before_authority_verifier_payload_or_send(self):
        calls = []
        gate = AdmissionGate(authority_revision=lambda: calls.append("authority"))
        self.denied(
            "PROVIDER_UNQUALIFIED",
            lambda: gate.dispatch(
                (binding(provider="modal"),),
                authorize=lambda: calls.append("paid-admit"),
                prepare=lambda: calls.append("load-secret"),
                send=lambda _: calls.append("send"),
            ),
        )
        self.assertEqual(calls, [])

    def test_worker_verified_json_is_rejected(self):
        self.verifier.transform = lambda _: {"verified": True}
        self.denied("APPRAISAL_INVALID", lambda: self.gate.admit(binding()))

    def test_every_session_identity_and_policy_field_is_bound(self):
        changes = dict(
            provider="other",
            factory_id="other",
            namespace="other",
            principal="other",
            job_id="other",
            session_id="other",
            execution_class="cpu-tools",
            audience="https://other.example",
            nonce="2" * 64,
            policy_epoch=2,
            policy_digest=OTHER,
            source_graph_digest=OTHER,
            workload_digest=OTHER,
            tool_policy_digest=OTHER,
            egress_policy_digest=OTHER,
            endpoint_key_digest=OTHER,
            model_digest=OTHER,
            account_id="other",
            provider_role="reviewer",
            request_digest=OTHER,
            commitment_id="other",
            reservation_id="other",
            attempt=2,
            original_lease_expires_at=151.0,
            profile_id="synthetic-other-v1",
        )
        for field, value in changes.items():
            with self.subTest(field=field):
                self.verifier.transform = lambda result, f=field, v=value: replace(
                    result, binding=replace(result.binding, **{f: v})
                )
                self.denied("SESSION_BINDING_MISMATCH", lambda: self.gate.admit(binding()))

    def test_wrong_issuer_platform_or_generation_rejected(self):
        for change, code in (
            (dict(issuer="worker-selected"), "VERIFIER_ISSUER_MISMATCH"),
            (dict(platform_digest=OTHER), "PLATFORM_MISMATCH"),
            (dict(authority_revision=0), "AUTHORITY_CHANGED"),
        ):
            with self.subTest(change=change):
                self.verifier.transform = lambda result, c=change: replace(result, **c)
                self.denied(code, lambda: self.gate.admit(binding()))

    def test_revocation_during_appraisal_rejected(self):
        def revoke(result):
            self.revision = 2
            return result

        self.verifier.transform = revoke
        self.denied("AUTHORITY_CHANGED", lambda: self.gate.admit(binding()))

    def test_verifier_failure_does_not_expose_error_content(self):
        def fail(_):
            raise RuntimeError("secret prompt details")

        self.verifier.transform = fail
        self.denied("VERIFIER_REJECTED_OR_UNAVAILABLE", lambda: self.gate.admit(binding()))

    def test_freshness_and_collateral_expiry(self):
        for change, code in (
            (dict(evaluated_at=111.0), "EVIDENCE_NOT_FRESH"),
            (dict(evaluated_at=70.0), "EVIDENCE_NOT_FRESH"),
            (dict(expires_at=110.0), "AUTHORIZATION_EXPIRED"),
            (dict(collateral_expires_at=110.0), "AUTHORIZATION_EXPIRED"),
            (dict(expires_at=161.0), "LEASE_INVALID"),
            (dict(evaluated_at=float("nan")), "LIFETIME_INVALID"),
            (dict(collateral_expires_at=float("inf")), "LIFETIME_INVALID"),
        ):
            with self.subTest(change=change):
                self.verifier.transform = lambda result, c=change: replace(result, **c)
                self.denied(code, lambda: self.gate.admit(binding()))

    def test_gpu_requires_cpu_and_composite_association(self):
        for change, code in (
            (dict(cpu_tee="kvm"), "CPU_TEE_REQUIRED"),
            (dict(gpu_tee=None), "COMPOSITE_GPU_TEE_REQUIRED"),
            (dict(cpu_gpu_link_digest=None), "COMPOSITE_GPU_TEE_REQUIRED"),
        ):
            with self.subTest(change=change):
                self.verifier.transform = lambda result, c=change: replace(result, **c)
                self.denied(code, lambda: self.gate.admit(binding()))

    def test_cpu_tool_profile_does_not_admit_gpu_work(self):
        profile = ProviderProfile(
            "synthetic-gpu-v1",
            "synthetic",
            D,
            "test-verifier",
            frozenset(("https://worker.example/v1",)),
            frozenset(("cpu-tools",)),
            60,
            30,
            self.verifier,
        )
        gate = AdmissionGate((profile,), authority_revision=lambda: 1)
        self.denied("EXECUTION_CLASS_UNQUALIFIED", lambda: gate.preflight(binding()))

    def test_synthetic_cpu_only_tool_appraisal_needs_no_gpu(self):
        self.verifier.transform = lambda result: replace(result, gpu_tee=None, cpu_gpu_link_digest=None)
        result = self.gate.admit(binding(execution_class="cpu-tools", model_digest=None))
        self.assertEqual(result.cpu_tee, "sev-snp")

    def test_empty_plan_and_invalid_bindings_fail_closed(self):
        self.denied(
            "EMPTY_PROTECTION_PLAN",
            lambda: self.gate.dispatch((), authorize=lambda: None, prepare=lambda: None, send=lambda _: None),
        )
        for change in (
            dict(policy_epoch=True),
            dict(model_digest=None),
            dict(nonce="short"),
            dict(audience="http://worker.example"),
            dict(audience="https://user@worker.example"),
            dict(audience="https://worker.example?secret=1"),
        ):
            with self.subTest(change=change), self.assertRaises(AdmissionDenied):
                self.gate.admit(binding(**change))

    def test_budget_or_off_refusal_blocks_secret_load(self):
        calls = []

        def stopped():
            raise AdmissionDenied("EXISTING_OFF_OR_BUDGET_GATE")

        self.denied(
            "EXISTING_OFF_OR_BUDGET_GATE",
            lambda: self.gate.dispatch(
                (binding(),),
                authorize=stopped,
                prepare=lambda: calls.append("secret"),
                send=lambda _: calls.append("send"),
            ),
        )
        self.assertEqual(calls, [])
        self.assertEqual(self.verifier.calls, 0)

    def test_expiry_or_revocation_while_preparing_blocks_send(self):
        for cause in ("expiry", "revocation"):
            with self.subTest(cause=cause):
                self.now, self.revision = 110.0, 1
                calls = []

                def prepare():
                    if cause == "expiry":
                        self.now = 130.0
                    else:
                        self.revision = 2
                    return "synthetic secret"

                with self.assertRaises(AdmissionDenied):
                    self.gate.dispatch(
                        (binding(),), authorize=lambda: None, prepare=prepare, send=lambda _: calls.append("send")
                    )
                self.assertEqual(calls, [])

    def test_separate_tool_worker_preflight_prevents_partial_secret_release(self):
        calls = []
        self.denied(
            "PROVIDER_UNQUALIFIED",
            lambda: self.gate.dispatch(
                (
                    binding(),
                    binding(
                        provider="modal",
                        profile_id="modal-unqualified-v1",
                        execution_class="cpu-tools",
                        model_digest=None,
                    ),
                ),
                authorize=lambda: calls.append("budget"),
                prepare=lambda: calls.append("secret"),
                send=lambda _: calls.append("send"),
            ),
        )
        self.assertEqual(calls, [])
        self.assertEqual(self.verifier.calls, 0)

    def test_synthetic_dispatch_rechecks_both_gates(self):
        calls = []
        result = self.gate.dispatch(
            (binding(),),
            authorize=lambda: calls.append("authorize"),
            prepare=lambda: calls.append("prepare") or "fake-payload",
            send=lambda p: calls.append("send") or p,
        )
        self.assertEqual(result, "fake-payload")
        self.assertEqual(calls, ["authorize", "authorize", "prepare", "authorize", "authorize", "send"])
        self.assertEqual(self.verifier.calls, 2)

    def test_attestation_cannot_extend_original_job_lease(self):
        self.denied("ORIGINAL_LEASE_EXCEEDED", lambda: self.gate.admit(binding(original_lease_expires_at=125.0)))

    def test_expiry_during_authorization_blocks_prepare_and_send(self):
        for delayed_call in (1, 2, 3, 4):
            with self.subTest(delayed_call=delayed_call):
                self.now = 110.0
                calls = []

                def authorize():
                    calls.append("authorize")
                    if calls.count("authorize") == delayed_call:
                        self.now = 130.0

                self.denied(
                    "AUTHORIZATION_EXPIRED",
                    lambda: self.gate.dispatch(
                        (binding(),),
                        authorize=authorize,
                        prepare=lambda: calls.append("prepare"),
                        send=lambda _: calls.append("send"),
                    ),
                )
                self.assertNotIn("send", calls)
                if delayed_call <= 2:
                    self.assertNotIn("prepare", calls)

    def test_cross_account_or_role_plan_is_refused(self):
        for change in (dict(account_id="other"), dict(provider_role="reviewer"), dict(job_id="other")):
            with self.subTest(change=change):
                self.denied(
                    "JOB_SCOPE_MISMATCH",
                    lambda: self.gate.dispatch(
                        (binding(), binding(execution_class="cpu-tools", **change)),
                        authorize=lambda: None,
                        prepare=lambda: None,
                        send=lambda _: None,
                    ),
                )
        self.assertEqual(self.verifier.calls, 0)

    def test_boolean_authority_and_nonfinite_time_cannot_admit(self):
        self.revision = True
        self.denied("AUTHORITY_UNAVAILABLE", lambda: self.gate.admit(binding()))
        self.revision = 1
        self.now = float("nan")
        self.denied("LIFETIME_INVALID", lambda: self.gate.admit(binding()))

    def test_boolean_authorization_is_not_permission(self):
        calls = []
        self.denied(
            "EXISTING_AUTHORIZATION_INVALID",
            lambda: self.gate.dispatch(
                (binding(),),
                authorize=lambda: False,
                prepare=lambda: calls.append("secret"),
                send=lambda _: calls.append("send"),
            ),
        )
        self.assertEqual(calls, [])

    def test_async_callback_is_refused_before_any_action(self):
        async def async_send(_):
            raise AssertionError("must not run")

        calls = []
        self.denied(
            "SYNCHRONOUS_CALLBACK_REQUIRED",
            lambda: self.gate.dispatch(
                (binding(),),
                authorize=lambda: calls.append("authorize"),
                prepare=lambda: calls.append("secret"),
                send=async_send,
            ),
        )
        self.assertEqual(calls, [])
        self.assertEqual(self.verifier.calls, 0)

    def test_wrapped_async_result_cannot_be_prepared_or_returned_as_send(self):
        async def deferred():
            raise AssertionError("must not run")

        calls = []
        self.denied(
            "DEFERRED_OPERATION_FORBIDDEN",
            lambda: self.gate.dispatch(
                (binding(),), authorize=lambda: None, prepare=lambda: deferred(), send=lambda _: calls.append("send")
            ),
        )
        self.assertEqual(calls, [])
        self.denied(
            "DEFERRED_OPERATION_FORBIDDEN",
            lambda: self.gate.dispatch(
                (binding(),), authorize=lambda: None, prepare=lambda: "fake", send=lambda _: deferred()
            ),
        )

    def test_async_callable_object_is_rejected_before_verifier(self):
        class DeferredAuthorization:
            async def __call__(self):
                raise AssertionError("must not run")

        calls = []
        self.denied(
            "DEFERRED_OPERATION_FORBIDDEN",
            lambda: self.gate.dispatch(
                (binding(),),
                authorize=DeferredAuthorization(),
                prepare=lambda: calls.append("secret"),
                send=lambda _: calls.append("send"),
            ),
        )
        self.assertEqual(calls, [])
        self.assertEqual(self.verifier.calls, 0)

    def test_separate_cpu_gpu_profiles_share_provider(self):
        cpu = ProviderProfile(
            "synthetic-cpu-v1",
            "synthetic",
            OTHER,
            "test-verifier",
            frozenset(("https://worker.example/v1",)),
            frozenset(("cpu-tools",)),
            60,
            30,
            self.verifier,
        )
        gpu = ProviderProfile(
            "synthetic-gpu-v1",
            "synthetic",
            D,
            "test-verifier",
            frozenset(("https://worker.example/v1",)),
            frozenset(("gpu-model",)),
            60,
            30,
            self.verifier,
        )
        self.verifier.transform = lambda result: replace(
            result, platform_digest=(OTHER if result.binding.profile_id == "synthetic-cpu-v1" else D)
        )
        gate = AdmissionGate((cpu, gpu), authority_revision=lambda: 1, clock=lambda: 110.0)
        requests = (
            binding(),
            binding(
                profile_id="synthetic-cpu-v1",
                execution_class="cpu-tools",
                session_id="tools-session",
                model_digest=None,
            ),
        )
        self.assertEqual(
            gate.dispatch(requests, authorize=lambda: None, prepare=lambda: "fake", send=lambda p: p), "fake"
        )

    def test_profile_cannot_be_reused_for_other_provider(self):
        self.denied("PROFILE_PROVIDER_MISMATCH", lambda: self.gate.preflight(binding(provider="modal")))

    def test_endpoint_outside_profile_is_rejected_before_verifier(self):
        self.denied("AUDIENCE_NOT_ADMITTED", lambda: self.gate.admit(binding(audience="https://other.example/v1")))
        self.assertEqual(self.verifier.calls, 0)

    def test_duplicate_session_requirement_is_rejected_before_verifier(self):
        self.denied(
            "DUPLICATE_SESSION_REQUIREMENT",
            lambda: self.gate.dispatch(
                (binding(), binding()), authorize=lambda: None, prepare=lambda: None, send=lambda _: None
            ),
        )
        self.assertEqual(self.verifier.calls, 0)

    def test_wrapped_generator_cannot_release_after_expiry(self):
        effects = []

        def delayed_send(payload):
            effects.append("released")
            yield payload

        self.denied(
            "DEFERRED_OPERATION_FORBIDDEN",
            lambda: self.gate.dispatch(
                (binding(),), authorize=lambda: None, prepare=lambda: "fake", send=lambda p: delayed_send(p)
            ),
        )
        self.now = 131.0
        self.assertEqual(effects, [])

    def test_async_generator_and_concurrent_future_are_refused(self):
        async def deferred_generator():
            yield "fake"

        for result in (deferred_generator(), Future()):
            with self.subTest(result=type(result).__name__):
                self.denied(
                    "DEFERRED_OPERATION_FORBIDDEN",
                    lambda: self.gate.dispatch(
                        (binding(),), authorize=lambda: None, prepare=lambda: "fake", send=lambda _: result
                    ),
                )

    def test_profile_requires_explicit_authority_callback(self):
        profile = ProviderProfile(
            "synthetic-gpu-v1",
            "synthetic",
            D,
            "test-verifier",
            frozenset(("https://worker.example/v1",)),
            frozenset(("gpu-model",)),
            60,
            30,
            self.verifier,
        )
        self.denied("AUTHORITY_REQUIRED", lambda: AdmissionGate((profile,)))
        self.assertEqual(self.verifier.calls, 0)

    def test_unavailable_authority_denies_before_verifier(self):
        def unavailable():
            raise RuntimeError("private database path and secret")

        self.gate._authority_revision = unavailable
        self.denied("AUTHORITY_UNAVAILABLE", lambda: self.gate.admit(binding()))
        self.assertEqual(self.verifier.calls, 0)

    def test_clock_failure_is_a_content_free_refusal(self):
        def unavailable():
            raise RuntimeError("private clock configuration")

        self.gate._clock = unavailable
        self.denied("CLOCK_UNAVAILABLE", lambda: self.gate.admit(binding()))


if __name__ == "__main__":
    unittest.main()
