"""Synthetic original-record/readiness refusal checks, not real host authority."""

import unittest
from dataclasses import replace

from drift.factory_admission import FactoryAdmission, RequestAdmissionDenied, request_body_digest
from drift.text_request import RequestContext

MANIFEST = "sha256:" + "a" * 64
REQUEST = "b" * 32


def ready_route():
    return {
        "manifest_digest": MANIFEST,
        "status": "complete",
        "covered_blocks": 3,
        "total_blocks": 3,
        "missing_blocks": [],
        "chat_ready": True,
        "peer_count": 3,
        "last_updated_age": 0.0,
        "replica_counts": [1, 1, 1],
    }


def original(body, **changes):
    return replace(
        FactoryAdmission(
            REQUEST, request_body_digest(body), MANIFEST, 3, "ordinary", 1, lambda: None, ready_route, lambda: None
        ),
        **changes,
    )


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.body = {"model": MANIFEST, "prompt": "private fixture", "n": 1, "stream": False}

    def test_unknown_missing_protected_or_mismatched_class_refuses_before_readers(self):
        reads = []
        for changes in (
            {"confidentiality_class": None},
            {"confidentiality_class": "required-confidential"},
            {"confidentiality_class": "unknown"},
            {"class_version": None},
            {"class_version": True},
            {"class_version": 2},
            {"body_digest": "c" * 64},
            {"manifest_digest": "sha256:" + "d" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(RequestAdmissionDenied):
                original(self.body, authority=lambda: reads.append("authority"), **changes).require_current(self.body)
        self.assertEqual(reads, [])

    def test_modified_body_and_auto_selector_cannot_reuse_original(self):
        admission = original(self.body)
        for changes in ({"prompt": "changed"}, {"model": "auto"}, {"stream": True}):
            with self.subTest(changes=changes), self.assertRaises(RequestAdmissionDenied):
                admission.require_current({**self.body, **changes})

    def test_every_block_must_have_fresh_coverage_for_exact_manifest(self):
        for changes in (
            {"covered_blocks": 2},
            {"covered_blocks": True},
            {"total_blocks": 4},
            {"missing_blocks": [1]},
            {"replica_counts": [1, 0, 1]},
            {"replica_counts": [1, 1]},
            {"replica_counts": [True, 1, 1]},
            {"chat_ready": False},
            {"status": "unknown"},
            {"peer_count": 0},
            {"last_updated_age": 6.0},
            {"last_updated_age": float("nan")},
            {"manifest_digest": "sha256:" + "c" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(RequestAdmissionDenied):
                original(self.body, route_snapshot=lambda: {**ready_route(), **changes}).require_current(self.body)

    def test_revoked_authority_and_lost_blocks_refuse_on_recheck(self):
        state = {"valid": True, "covered": 3}

        def authority():
            if not state["valid"]:
                raise RuntimeError("do not expose this secret")

        admission = original(
            self.body, authority=authority, route_snapshot=lambda: {**ready_route(), "covered_blocks": state["covered"]}
        )
        admission.require_current(self.body)
        state["covered"] = 2
        with self.assertRaises(RequestAdmissionDenied):
            admission.require_current(self.body)
        state.update(covered=3, valid=False)
        with self.assertRaisesRegex(RequestAdmissionDenied, "^Original request admission denied$"):
            admission.require_current(self.body)

    def test_deferred_authority_is_not_advanced(self):
        effects = []

        async def deferred():
            effects.append("coroutine")

        def generator():
            effects.append("generator")
            yield None

        for callback in (deferred, generator, lambda: True):
            with self.assertRaises(RequestAdmissionDenied):
                original(self.body, authority=callback).require_current(self.body)
        self.assertEqual(effects, [])

    def test_nonfinite_body_has_no_digest(self):
        with self.assertRaises(RequestAdmissionDenied):
            request_body_digest({"temperature": float("nan")})

    def test_one_context_cannot_dispatch_twice_even_after_success(self):
        context = RequestContext.start(
            60, request_id=REQUEST, single_attempt=True, dispatch_guard=lambda: None, dispatch_claim=lambda: None
        )
        context.begin_dispatch()
        self.assertTrue(context.dispatch_started)
        self.assertEqual(context.request_id, REQUEST)
        with self.assertRaises(RequestAdmissionDenied):
            context.begin_dispatch()

    def test_single_attempt_context_requires_guard(self):
        with self.assertRaises(ValueError):
            RequestContext.start(60, single_attempt=True)

    def test_two_contexts_require_one_shared_original_dispatch_claim(self):
        state = {"claimed": False}

        def claim():
            if state["claimed"]:
                raise RequestAdmissionDenied()
            state["claimed"] = True

        contexts = [
            RequestContext.start(
                60, request_id=REQUEST, single_attempt=True, dispatch_guard=lambda: None, dispatch_claim=claim
            )
            for _ in range(2)
        ]
        contexts[0].begin_dispatch()
        with self.assertRaises(RequestAdmissionDenied):
            contexts[1].begin_dispatch()
        self.assertFalse(contexts[1].dispatch_started)

    def test_authority_revoked_by_claim_refuses_inference_entry_without_releasing_claim(self):
        state = {"valid": True, "claimed": False}

        def guard():
            if not state["valid"]:
                raise RequestAdmissionDenied()

        def claim():
            state.update(valid=False, claimed=True)

        context = RequestContext.start(60, single_attempt=True, dispatch_guard=guard, dispatch_claim=claim)
        with self.assertRaises(RequestAdmissionDenied):
            context.begin_dispatch()
        self.assertFalse(context.dispatch_started)
        self.assertTrue(state["claimed"])
