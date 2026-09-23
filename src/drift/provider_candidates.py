"""Pinned, deliberately unavailable whole-model provider candidates.

These records are source metadata, not runners or qualification evidence.  They
perform no I/O at import and cannot promote either required model to available.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Mapping

from drift.inference_provider import REQUIRED_MODEL_IDS, Availability, ProviderProfile

_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_GATE = re.compile(r"[a-z][a-z0-9_]{0,95}")
_PATH_PART = re.compile(r"[A-Za-z0-9_.+-]{1,128}")
_RELEASE = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_COMMON_GATES = frozenset(
    {
        "compatibility_execution",
        "hardware_qualification",
        "installed_workflow_qualification",
        "prompt_encoder_revision",
        "rights_approval",
        "runtime_manifest_digest",
        "weight_artifact_manifest",
    }
)


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid provider candidate metadata")


def _text(value: object, *, maximum: int = 256) -> bool:
    return (
        type(value) is str and 0 < len(value) <= maximum and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _relative_path(value: object) -> bool:
    if not _text(value, maximum=1024) or ":" in value or "\\" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} and _PATH_PART.fullmatch(part) is not None for part in parts)


@dataclass(frozen=True)
class FilePin:
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _require(_relative_path(self.path))
        _require(type(self.sha256) is str and _HEX64.fullmatch(self.sha256) is not None)
        _require(type(self.size_bytes) is int and 0 < self.size_bytes < 2**63)


@dataclass(frozen=True)
class ServingEvidencePin:
    repository: str
    revision: str
    path: str
    sha256: str
    size_bytes: int
    engine_repository: str
    engine_revision: str

    def __post_init__(self) -> None:
        _require(_text(self.repository) and _text(self.engine_repository))
        _require(
            type(self.revision) is str
            and type(self.engine_revision) is str
            and _HEX40.fullmatch(self.revision) is not None
            and _HEX40.fullmatch(self.engine_revision) is not None
        )
        FilePin(self.path, self.sha256, self.size_bytes)


@dataclass(frozen=True)
class BackendPin:
    backend_id: str
    repository: str
    release: str
    source_revision: str
    license_sha256: str
    serving_evidence: ServingEvidencePin

    def __post_init__(self) -> None:
        _require(_text(self.backend_id) and _text(self.repository) and _text(self.release))
        _require(
            type(self.release) is str
            and _RELEASE.fullmatch(self.release) is not None
            and self.backend_id == "vllm-" + self.release
        )
        _require(type(self.source_revision) is str and _HEX40.fullmatch(self.source_revision) is not None)
        _require(type(self.license_sha256) is str and _HEX64.fullmatch(self.license_sha256) is not None)
        _require(type(self.serving_evidence) is ServingEvidencePin)


@dataclass(frozen=True)
class UnavailableProviderCandidate:
    schema_version: int
    model_id: str
    model_revision: str
    architecture: str
    model_type: str
    declared_max_positions: int
    quantization: str
    config: FilePin
    tokenizer_config: FilePin
    generation_config: FilePin | None
    license_identifier: str
    license_file: FilePin
    backend: BackendPin
    runtime_manifest_digest: None
    weight_artifact_manifest_digest: None
    prompt_encoder_revision: None
    rights_approval_id: None
    hardware_qualification_id: None
    unresolved_gates: tuple[str, ...]

    def __post_init__(self) -> None:
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(type(self.model_id) is str and self.model_id in REQUIRED_MODEL_IDS)
        _require(type(self.model_revision) is str and _HEX40.fullmatch(self.model_revision) is not None)
        _require(_text(self.architecture) and _text(self.model_type) and _text(self.quantization))
        _require(type(self.declared_max_positions) is int and 0 < self.declared_max_positions < 2**63)
        _require(type(self.config) is FilePin and self.config.path == "config.json")
        _require(type(self.tokenizer_config) is FilePin and self.tokenizer_config.path == "tokenizer_config.json")
        _require(
            self.generation_config is None
            or (type(self.generation_config) is FilePin and self.generation_config.path == "generation_config.json")
        )
        _require(_text(self.license_identifier) and type(self.license_file) is FilePin)
        _require(self.license_file.path == "LICENSE" and type(self.backend) is BackendPin)
        _require(
            self.runtime_manifest_digest is None
            and self.weight_artifact_manifest_digest is None
            and self.prompt_encoder_revision is None
            and self.rights_approval_id is None
            and self.hardware_qualification_id is None
        )
        _require(type(self.unresolved_gates) is tuple and 1 <= len(self.unresolved_gates) <= 32)
        _require(all(type(gate) is str and _GATE.fullmatch(gate) is not None for gate in self.unresolved_gates))
        _require(
            self.unresolved_gates == tuple(sorted(set(self.unresolved_gates)))
            and _COMMON_GATES <= set(self.unresolved_gates)
        )

    def identity_document(self) -> dict[str, object]:
        """Return a detached complete identity document for deterministic evidence."""
        return asdict(self)

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.identity_document(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def profile_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def provider_profile(self) -> ProviderProfile:
        return ProviderProfile(
            profile_id="candidate/sha256:" + self.profile_digest,
            model_id=self.model_id,
            availability=Availability.UNAVAILABLE,
        )


_VLLM = dict(
    backend_id="vllm-v0.30.0",
    repository="vllm-project/vllm",
    release="v0.30.0",
    source_revision="ced6857afa0ea7b2e3f0846a62e1394e90f15607",
    license_sha256="c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
)

DEEPSEEK_V41_FLASH_CANDIDATE = UnavailableProviderCandidate(
    schema_version=1,
    model_id="deepseek-ai/DeepSeek-V4.1-Flash",
    model_revision="dba1be0a40aa45a94ad051997016db3960a90277",
    architecture="DeepseekV41ForCausalLM",
    model_type="deepseek_v41",
    declared_max_positions=1_048_576,
    quantization="fp8-dynamic-32x32+fp4-experts",
    config=FilePin("config.json", "8be45ce0476004a3f529fd896115a4a2e800a129ad2d3ec05b16050f52e21879", 3_311),
    tokenizer_config=FilePin(
        "tokenizer_config.json", "6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547", 801
    ),
    generation_config=None,
    license_identifier="MIT",
    license_file=FilePin("LICENSE", "f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040", 1_084),
    backend=BackendPin(
        **_VLLM,
        serving_evidence=ServingEvidencePin(
            repository="vllm-project/recipes",
            revision="7f364beb3c8bad7670bdbf41d730a53bec7ce0ba",
            path="models/deepseek-ai/DeepSeek-V4.1-Flash.yaml",
            sha256="d280bfc90e7cf94eb3c549f1b85a691aacdea75d41218d2012be968e4cd00e39",
            size_bytes=33_514,
            engine_repository="vllm-project/vllm",
            engine_revision="d1b4028d7eaed13e91720e164e42dbc80d8d9c3b",
        ),
    ),
    runtime_manifest_digest=None,
    weight_artifact_manifest_digest=None,
    prompt_encoder_revision=None,
    rights_approval_id=None,
    hardware_qualification_id=None,
    unresolved_gates=tuple(
        sorted(
            _COMMON_GATES
            | {
                "multimodal_qualification",
                "reasoning_tool_parser_qualification",
                "vllm_release_architecture_support",
            }
        )
    ),
)

GLM_53_CANDIDATE = UnavailableProviderCandidate(
    schema_version=1,
    model_id="zai-org/GLM-5.3",
    model_revision="aca966e4e02791568aa6a4ced368624b3d897f42",
    architecture="GlmMoeDsaForCausalLM",
    model_type="glm_moe_dsa",
    declared_max_positions=1_048_576,
    quantization="fp8-dynamic-128x128-e4m3",
    config=FilePin("config.json", "3ac72612095574542f7fff847ada8e59d9199dd8af44bdf625d7e02615572e69", 29_464),
    tokenizer_config=FilePin(
        "tokenizer_config.json", "98b1271574f41abf89427ae2dda030d94dc9478f0edc5a8bd240db213c6fd5fc", 761
    ),
    generation_config=FilePin(
        "generation_config.json", "ac76b43d8683d3b930126870fc8be73d8679308fe752fa1f381096d8354f6a55", 194
    ),
    license_identifier="GLM-5.3 License",
    license_file=FilePin("LICENSE", "96e1622099fc9d6b70c9760f007d99e66d7497eec636b63c60fe208401e9170c", 4_263),
    backend=BackendPin(
        **_VLLM,
        serving_evidence=ServingEvidencePin(
            repository="vllm-project/vllm-project.github.io",
            revision="f6552f723bb4d02e110631e86c90fee5ebfafea5",
            path="_posts/2026-09-08-glm53-part1-hybrid-sparse-offloading.md",
            sha256="2e95848c4e4f60d6cede04151f07763512df2acf004df3b2a2030902b43fc4ee",
            size_bytes=16_318,
            engine_repository="neuralmagic/vllm",
            engine_revision="e8ef1e07bd2f174bebfe34c3a3e35e952931efb1",
        ),
    ),
    runtime_manifest_digest=None,
    weight_artifact_manifest_digest=None,
    prompt_encoder_revision=None,
    rights_approval_id=None,
    hardware_qualification_id=None,
    unresolved_gates=tuple(
        sorted(
            _COMMON_GATES
            | {
                "glm_commercial_license_determination",
                "offload_mtp_parser_qualification",
                "remote_code_elimination_or_review",
            }
        )
    ),
)

PROVIDER_CANDIDATES: Mapping[str, UnavailableProviderCandidate] = MappingProxyType(
    {candidate.model_id: candidate for candidate in (DEEPSEEK_V41_FLASH_CANDIDATE, GLM_53_CANDIDATE)}
)
PROVIDER_PROFILES: Mapping[str, ProviderProfile] = MappingProxyType(
    {model_id: candidate.provider_profile for model_id, candidate in PROVIDER_CANDIDATES.items()}
)

__all__ = [
    "BackendPin",
    "DEEPSEEK_V41_FLASH_CANDIDATE",
    "FilePin",
    "GLM_53_CANDIDATE",
    "PROVIDER_CANDIDATES",
    "PROVIDER_PROFILES",
    "ServingEvidencePin",
    "UnavailableProviderCandidate",
]
