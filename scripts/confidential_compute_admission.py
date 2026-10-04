"""Portable host policy seam; no hardware verifier or protected provider is installed.

FACTORY and O may evaluate this source before adopting it. Never construct a
ProviderProfile from a worker response, request body or environment flag. The
host's verifier adapter must authenticate and appraise real evidence with vetted
upstream libraries. This module does not verify quotes, signatures or TLS.
"""

from __future__ import annotations

import inspect
import math
import re
import time
from concurrent.futures import Future
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Protocol, TypeVar
from urllib.parse import urlsplit

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CLASSES = frozenset(("cpu-tools", "gpu-model"))
T = TypeVar("T")


class AdmissionDenied(RuntimeError):
    """Content-free refusal safe for an operational receipt."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AdmissionDenied(code)


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _synchronous(value: T) -> T:
    if (
        inspect.isawaitable(value)
        or inspect.isgenerator(value)
        or inspect.isasyncgen(value)
        or isinstance(value, Future)
    ):
        if inspect.iscoroutine(value) or inspect.isgenerator(value):
            value.close()
        raise AdmissionDenied("DEFERRED_OPERATION_FORBIDDEN")
    return value


@dataclass(frozen=True)
class Binding:
    """Metadata only; nonce and endpoint key must originate from trusted code."""

    provider: str
    factory_id: str
    namespace: str
    principal: str
    job_id: str
    session_id: str
    execution_class: str
    audience: str
    nonce: str
    policy_epoch: int
    policy_digest: str
    source_graph_digest: str
    workload_digest: str
    tool_policy_digest: str
    egress_policy_digest: str
    endpoint_key_digest: str
    account_id: str
    provider_role: str
    request_digest: str
    commitment_id: str
    reservation_id: str
    attempt: int
    original_lease_expires_at: float
    profile_id: str
    model_digest: str | None = None

    def validate(self) -> None:
        for value in (
            self.provider,
            self.factory_id,
            self.namespace,
            self.principal,
            self.job_id,
            self.session_id,
            self.account_id,
            self.commitment_id,
            self.reservation_id,
            self.profile_id,
        ):
            _require(type(value) is str and _ID.fullmatch(value) is not None, "BINDING_INVALID")
        _require(type(self.execution_class) is str and self.execution_class in _CLASSES, "EXECUTION_CLASS_INVALID")
        _require(
            type(self.provider_role) is str and self.provider_role in ("developer", "reviewer"),
            "PROVIDER_ROLE_INVALID",
        )
        _require(type(self.attempt) is int and self.attempt > 0, "ATTEMPT_INVALID")
        _require(
            _finite(self.original_lease_expires_at) and self.original_lease_expires_at > 0, "ORIGINAL_LEASE_INVALID"
        )
        _require(type(self.policy_epoch) is int and self.policy_epoch > 0, "POLICY_EPOCH_INVALID")
        _require(type(self.nonce) is str and re.fullmatch(r"[0-9a-f]{64}", self.nonce) is not None, "CHALLENGE_INVALID")
        for value in (
            self.policy_digest,
            self.source_graph_digest,
            self.workload_digest,
            self.tool_policy_digest,
            self.egress_policy_digest,
            self.endpoint_key_digest,
            self.request_digest,
        ):
            _require(type(value) is str and _DIGEST.fullmatch(value) is not None, "DIGEST_INVALID")
        _require(
            self.model_digest is None
            or (type(self.model_digest) is str and _DIGEST.fullmatch(self.model_digest) is not None),
            "MODEL_DIGEST_INVALID",
        )
        _require(self.execution_class != "gpu-model" or self.model_digest is not None, "MODEL_DIGEST_REQUIRED")
        _require(type(self.audience) is str and not any(c.isspace() for c in self.audience), "AUDIENCE_INVALID")
        try:
            url = urlsplit(self.audience)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
            )
            _ = url.port
        except ValueError:
            valid = False
        _require(valid, "AUDIENCE_INVALID")


def _binding_snapshot(binding: Binding) -> Binding:
    # A frozen dataclass can still be changed through object.__setattr__ by
    # code holding an alias; compare private values across callback boundaries.
    _require(type(binding) is Binding, "BINDING_INVALID")
    snapshot = replace(binding)
    Binding.validate(snapshot)
    return snapshot


@dataclass(frozen=True)
class Appraisal:
    """Return type of a trusted host adapter, never a public JSON admission token.

    The adapter must validate issuer/signature, chain/collateral/revocation,
    measured policy/artifacts, challenge and guest-held key possession, then
    bind the actual assigned CPU/GPU path. Arbitrary worker claims are inputs to
    that adapter, not this type. A synthetic instance proves policy tests only.
    """

    binding: Binding
    issuer: str
    platform_digest: str
    evaluated_at: float
    expires_at: float
    collateral_expires_at: float
    authority_revision: int
    cpu_tee: str
    gpu_tee: str | None
    cpu_gpu_link_digest: str | None


class Verifier(Protocol):
    def appraise(self, binding: Binding) -> Appraisal:
        """Fail on unavailable, revoked, malformed or unauthenticated evidence."""


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    provider: str
    platform_digest: str
    verifier_issuer: str
    audiences: frozenset[str]
    execution_classes: frozenset[str]
    max_lease_seconds: int
    max_evidence_age_seconds: int
    verifier: Verifier


class AdmissionGate:
    """Host-owned configuration and authority callback; default denies all.

    No Modal profile is installed: public evidence has not qualified its CPU
    or GPU TEE path. Host isolation, GPU SKU and TLS do not create a profile.
    authority_revision reads the current trusted policy/revocation generation.
    """

    def __init__(
        self,
        profiles: Iterable[ProviderProfile] = (),
        *,
        authority_revision: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._profiles: dict[str, ProviderProfile] = {}
        self._authority_revision = authority_revision
        self._clock = clock
        for profile in profiles:
            _require(type(profile) is ProviderProfile, "PROFILE_INVALID")
            _require(
                type(profile.profile_id) is str
                and _ID.fullmatch(profile.profile_id) is not None
                and profile.profile_id not in self._profiles,
                "PROFILE_INVALID",
            )
            _require(type(profile.provider) is str and _ID.fullmatch(profile.provider) is not None, "PROFILE_INVALID")
            _require(
                type(profile.platform_digest) is str and _DIGEST.fullmatch(profile.platform_digest) is not None,
                "PROFILE_INVALID",
            )
            _require(
                type(profile.verifier_issuer) is str and _ID.fullmatch(profile.verifier_issuer) is not None,
                "PROFILE_INVALID",
            )
            _require(
                type(profile.audiences) is frozenset
                and bool(profile.audiences)
                and all(type(audience) is str for audience in profile.audiences),
                "PROFILE_INVALID",
            )
            _require(
                type(profile.execution_classes) is frozenset
                and bool(profile.execution_classes)
                and all(type(execution_class) is str for execution_class in profile.execution_classes)
                and profile.execution_classes <= _CLASSES,
                "PROFILE_INVALID",
            )
            for limit in (profile.max_lease_seconds, profile.max_evidence_age_seconds):
                _require(type(limit) is int and 0 < limit <= 3600, "PROFILE_INVALID")
            _require(callable(getattr(profile.verifier, "appraise", None)), "VERIFIER_REQUIRED")
            self._profiles[profile.profile_id] = profile
        if self._profiles:
            _require(callable(authority_revision), "AUTHORITY_REQUIRED")

    def _read_authority(self) -> int:
        try:
            revision = self._authority_revision()
        except Exception:
            raise AdmissionDenied("AUTHORITY_UNAVAILABLE") from None
        _require(type(revision) is int and revision >= 0, "AUTHORITY_UNAVAILABLE")
        return revision

    def preflight(self, binding: Binding) -> ProviderProfile:
        """Call before provisioning, allocating paid work or accessing secrets."""
        expected = _binding_snapshot(binding)
        profile = self._profiles.get(expected.profile_id)
        _require(profile is not None, "PROVIDER_UNQUALIFIED")
        _require(expected.provider == profile.provider, "PROFILE_PROVIDER_MISMATCH")
        _require(expected.audience in profile.audiences, "AUDIENCE_NOT_ADMITTED")
        _require(expected.execution_class in profile.execution_classes, "EXECUTION_CLASS_UNQUALIFIED")
        return profile

    def admit(self, binding: Binding) -> Appraisal:
        """Ask the trusted adapter to appraise before each sensitive operation."""
        expected = _binding_snapshot(binding)
        profile = self.preflight(expected)
        revision = self._read_authority()
        try:
            result = _synchronous(profile.verifier.appraise(replace(expected)))
        except Exception:
            raise AdmissionDenied("VERIFIER_REJECTED_OR_UNAVAILABLE") from None
        _require(type(result) is Appraisal, "APPRAISAL_INVALID")
        _require(type(result.binding) is Binding, "APPRAISAL_INVALID")
        appraisal = replace(result, binding=_binding_snapshot(result.binding))
        _require(_binding_snapshot(binding) == expected, "SESSION_BINDING_MISMATCH")
        validated = self._validate(expected, profile, appraisal, revision)
        _require(_binding_snapshot(binding) == expected, "SESSION_BINDING_MISMATCH")
        return validated

    def _validate(self, binding: Binding, profile: ProviderProfile, result: Appraisal, revision: int) -> Appraisal:
        _require(type(result) is Appraisal, "APPRAISAL_INVALID")
        _require(type(result.binding) is Binding, "APPRAISAL_INVALID")
        Binding.validate(result.binding)
        _require(result.binding == binding, "SESSION_BINDING_MISMATCH")
        _require(type(result.issuer) is str and result.issuer == profile.verifier_issuer, "VERIFIER_ISSUER_MISMATCH")
        _require(
            type(result.platform_digest) is str and result.platform_digest == profile.platform_digest,
            "PLATFORM_MISMATCH",
        )
        current_revision = self._read_authority()
        _require(
            type(current_revision) is int
            and type(result.authority_revision) is int
            and result.authority_revision == revision == current_revision,
            "AUTHORITY_CHANGED",
        )
        try:
            now = self._clock()
        except Exception:
            raise AdmissionDenied("CLOCK_UNAVAILABLE") from None
        _require(
            all(_finite(v) for v in (now, result.evaluated_at, result.expires_at, result.collateral_expires_at)),
            "LIFETIME_INVALID",
        )
        _require(0 <= now - result.evaluated_at <= profile.max_evidence_age_seconds, "EVIDENCE_NOT_FRESH")
        _require(
            result.evaluated_at < result.expires_at <= result.evaluated_at + profile.max_lease_seconds, "LEASE_INVALID"
        )
        _require(result.expires_at <= binding.original_lease_expires_at, "ORIGINAL_LEASE_EXCEEDED")
        _require(now < min(result.expires_at, result.collateral_expires_at), "AUTHORIZATION_EXPIRED")
        _require(type(result.cpu_tee) is str and result.cpu_tee in ("tdx", "sev-snp"), "CPU_TEE_REQUIRED")
        if binding.execution_class == "gpu-model":
            _require(
                type(result.gpu_tee) is str
                and result.gpu_tee == "nvidia-cc"
                and type(result.cpu_gpu_link_digest) is str
                and _DIGEST.fullmatch(result.cpu_gpu_link_digest) is not None,
                "COMPOSITE_GPU_TEE_REQUIRED",
            )
        _require(self._read_authority() == revision, "AUTHORITY_CHANGED")
        return result

    def _require_all_current(self, bindings: tuple[Binding, ...], appraisals: list[Appraisal]) -> None:
        """Check every session against one clock sample before a sensitive callback.

        Per-session validation alone leaves an earlier appraisal stale while a
        later session is being checked. This is a synchronous boundary check,
        not an atomic guarantee across a later physical network send.
        """
        try:
            now = self._clock()
        except Exception:
            raise AdmissionDenied("CLOCK_UNAVAILABLE") from None
        _require(_finite(now), "LIFETIME_INVALID")
        for binding, result in zip(bindings, appraisals):
            profile = self.preflight(binding)
            _require(0 <= now - result.evaluated_at <= profile.max_evidence_age_seconds, "EVIDENCE_NOT_FRESH")
            _require(now < min(result.expires_at, result.collateral_expires_at), "AUTHORIZATION_EXPIRED")
        revision = self._read_authority()
        _require(all(result.authority_revision == revision for result in appraisals), "AUTHORITY_CHANGED")

    def dispatch(
        self,
        bindings: Iterable[Binding],
        *,
        authorize: Callable[[], None],
        prepare: Callable[[], T],
        send: Callable[[T], object],
    ) -> object:
        """Synchronous example seam, not an atomic distributed send transaction.

        authorize must enforce existing budget/OFF/role/writer/capture gates and
        raise on refusal. prepare may load payload/secrets only after admission.
        Both gates are rechecked after prepare. Async consumers must implement
        these checks again at their actual transport boundary; never serialize
        an Appraisal as a transferable bearer grant.
        """
        originals = tuple(bindings)
        requirements = tuple(_binding_snapshot(binding) for binding in originals)
        _require(bool(requirements), "EMPTY_PROTECTION_PLAN")
        _require(
            all(
                callable(callback)
                and not inspect.iscoroutinefunction(callback)
                and not inspect.isgeneratorfunction(callback)
                and not inspect.isasyncgenfunction(callback)
                for callback in (authorize, prepare, send)
            ),
            "SYNCHRONOUS_CALLBACK_REQUIRED",
        )
        for binding in requirements:
            self.preflight(binding)
        first = requirements[0]
        scope = lambda b: (
            b.factory_id,
            b.namespace,
            b.principal,
            b.job_id,
            b.account_id,
            b.provider_role,
            b.request_digest,
            b.commitment_id,
            b.reservation_id,
            b.attempt,
            b.original_lease_expires_at,
        )
        _require(all(scope(b) == scope(first) for b in requirements), "JOB_SCOPE_MISMATCH")
        _require(
            len({b.session_id for b in requirements}) == len(requirements),
            "DUPLICATE_SESSION_REQUIREMENT",
        )

        def assert_plan_stable() -> None:
            _require(
                all(_binding_snapshot(original) == expected for original, expected in zip(originals, requirements)),
                "SESSION_BINDING_MISMATCH",
            )

        def authorize_stable() -> None:
            _require(_synchronous(authorize()) is None, "EXISTING_AUTHORIZATION_INVALID")
            assert_plan_stable()

        authorize_stable()
        appraisals = [self.admit(b) for b in requirements]
        assert_plan_stable()
        authorize_stable()
        for binding, result in zip(requirements, appraisals):
            self._validate(binding, self.preflight(binding), result, result.authority_revision)
        assert_plan_stable()
        self._require_all_current(requirements, appraisals)
        assert_plan_stable()
        payload = _synchronous(prepare())
        assert_plan_stable()
        authorize_stable()
        appraisals = [self.admit(b) for b in requirements]
        assert_plan_stable()
        authorize_stable()
        for binding, result in zip(requirements, appraisals):
            self._validate(binding, self.preflight(binding), result, result.authority_revision)
        assert_plan_stable()
        self._require_all_current(requirements, appraisals)
        assert_plan_stable()
        return _synchronous(send(payload))
