from dataclasses import FrozenInstanceError, replace

import pytest

from drift import inference_provider as provider, provider_candidates as candidates


def test_exact_two_model_profiles_are_digest_bound_and_unavailable():
    assert set(candidates.PROVIDER_CANDIDATES) == {
        provider.DEEPSEEK_V41_FLASH,
        provider.GLM_53,
    }
    assert set(candidates.PROVIDER_PROFILES) == set(candidates.PROVIDER_CANDIDATES)
    assert "zai-org/GLM-5.3-Flash" not in candidates.PROVIDER_CANDIDATES

    for model_id, candidate in candidates.PROVIDER_CANDIDATES.items():
        profile = candidates.PROVIDER_PROFILES[model_id]
        assert profile == candidate.provider_profile
        assert profile.profile_id == "candidate/sha256:" + candidate.profile_digest
        assert profile.model_id == model_id
        assert profile.availability is provider.Availability.UNAVAILABLE
        assert profile.supported_features == frozenset()
        assert profile.qualification_id is None

    with pytest.raises(TypeError):
        candidates.PROVIDER_CANDIDATES["other/model"] = candidates.GLM_53_CANDIDATE


def test_pins_match_research_and_keep_full_glm_distinct_from_flash():
    deepseek = candidates.DEEPSEEK_V41_FLASH_CANDIDATE
    assert deepseek.model_revision == "dba1be0a40aa45a94ad051997016db3960a90277"
    assert deepseek.config.sha256 == "8be45ce0476004a3f529fd896115a4a2e800a129ad2d3ec05b16050f52e21879"
    assert deepseek.tokenizer_config.sha256 == "6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547"
    assert deepseek.generation_config is None
    assert deepseek.license_file.sha256 == "f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040"
    assert {item.path: item.sha256 for item in deepseek.prompt_source_files} == {
        "encoding/README.md": "a2f0fc3baea318c9cfbceca68cbfe50d37cf7da6605ace887f33148bcff7e3ae",
        "encoding/encoding.py": "502bdaec8a3fd88ebc24c4721a7038fbe42f2063c664638127056107920035c1",
        "encoding/test_encoding.py": "4a470892dad828459958cebfad55da598aafcb7a5878764ccbfc3ab060399d06",
    }

    glm = candidates.GLM_53_CANDIDATE
    assert glm.model_id == "zai-org/GLM-5.3" and "Flash" not in glm.model_id
    assert glm.model_revision == "aca966e4e02791568aa6a4ced368624b3d897f42"
    assert glm.architecture == "GlmMoeDsaForCausalLM" and glm.model_type == "glm_moe_dsa"
    assert glm.config.sha256 == "3ac72612095574542f7fff847ada8e59d9199dd8af44bdf625d7e02615572e69"
    assert glm.tokenizer_config.sha256 == "98b1271574f41abf89427ae2dda030d94dc9478f0edc5a8bd240db213c6fd5fc"
    assert glm.generation_config.sha256 == "ac76b43d8683d3b930126870fc8be73d8679308fe752fa1f381096d8354f6a55"
    assert glm.license_file.sha256 == "96e1622099fc9d6b70c9760f007d99e66d7497eec636b63c60fe208401e9170c"
    assert glm.prompt_source_files == (
        candidates.FilePin(
            "chat_template.jinja", "3740abcea51c45830cb3ca562084ad5fb2ef53589376f73332e9886f93ade41c", 10_734
        ),
    )


def test_backend_release_source_and_serving_evidence_are_exactly_pinned():
    for candidate in candidates.PROVIDER_CANDIDATES.values():
        assert candidate.backend.backend_id == "vllm-v0.30.0"
        assert candidate.backend.release == "v0.30.0"
        assert candidate.backend.source_revision == "ced6857afa0ea7b2e3f0846a62e1394e90f15607"
        assert candidate.backend.license_sha256 == ("c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4")

    assert candidates.DEEPSEEK_V41_FLASH_CANDIDATE.backend.serving_evidence.revision == (
        "7f364beb3c8bad7670bdbf41d730a53bec7ce0ba"
    )
    glm_evidence = candidates.GLM_53_CANDIDATE.backend.serving_evidence
    assert glm_evidence.revision == "f6552f723bb4d02e110631e86c90fee5ebfafea5"
    assert glm_evidence.engine_repository == "neuralmagic/vllm"
    assert glm_evidence.engine_revision == "e8ef1e07bd2f174bebfe34c3a3e35e952931efb1"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: replace(value, model_revision="0" * 40),
        lambda value: replace(value, config=replace(value.config, sha256="0" * 64)),
        lambda value: replace(value, tokenizer_config=replace(value.tokenizer_config, sha256="0" * 64)),
        lambda value: replace(
            value,
            prompt_source_files=(
                replace(value.prompt_source_files[0], sha256="0" * 64),
                *value.prompt_source_files[1:],
            ),
        ),
        lambda value: replace(value, license_file=replace(value.license_file, sha256="0" * 64)),
        lambda value: replace(
            value,
            backend=replace(value.backend, backend_id="vllm-v0.30.1", release="v0.30.1"),
        ),
        lambda value: replace(value, backend=replace(value.backend, source_revision="0" * 40)),
        lambda value: replace(value, unresolved_gates=tuple(sorted((*value.unresolved_gates, "new_gate")))),
    ],
)
def test_every_material_identity_change_changes_canonical_profile_digest(mutate):
    original = candidates.DEEPSEEK_V41_FLASH_CANDIDATE
    changed = mutate(original)
    assert changed.canonical_bytes() != original.canonical_bytes()
    assert changed.profile_digest != original.profile_digest
    assert changed.provider_profile.profile_id != original.provider_profile.profile_id


def test_canonical_document_is_complete_detached_and_deterministic():
    candidate = candidates.GLM_53_CANDIDATE
    first = candidate.identity_document()
    first["backend"]["release"] = "changed-outside-record"
    assert candidate.identity_document()["backend"]["release"] == "v0.30.0"
    assert candidate.canonical_bytes() == candidate.canonical_bytes()
    assert candidate.profile_digest == candidate.provider_profile.profile_id.removeprefix("candidate/sha256:")
    assert candidates.DEEPSEEK_V41_FLASH_CANDIDATE.profile_digest == (
        "08a2300e8e3600fef075118b83c8ef6e0b0ec6779a801fca1ddbe1af9e281ee2"
    )
    assert candidate.profile_digest == "820c52463968e66476051aaca854a97a13278277e0c238a4c6d3ef2da9725425"


def test_all_missing_authority_and_qualification_inputs_remain_explicit_gates():
    required = {
        "runtime_manifest_digest",
        "prompt_encoder_revision",
        "rights_approval",
        "hardware_qualification",
        "weight_artifact_manifest",
    }
    for candidate in candidates.PROVIDER_CANDIDATES.values():
        assert required <= set(candidate.unresolved_gates)
        assert candidate.runtime_manifest_digest is None
        assert candidate.weight_artifact_manifest_digest is None
        assert candidate.prompt_encoder_revision is None
        assert candidate.rights_approval_id is None
        assert candidate.hardware_qualification_id is None

    assert "vllm_release_architecture_support" in candidates.DEEPSEEK_V41_FLASH_CANDIDATE.unresolved_gates
    assert "glm_commercial_license_determination" in candidates.GLM_53_CANDIDATE.unresolved_gates
    assert "remote_code_elimination_or_review" in candidates.GLM_53_CANDIDATE.unresolved_gates


def test_candidates_have_no_availability_promotion_path():
    candidate = candidates.DEEPSEEK_V41_FLASH_CANDIDATE
    with pytest.raises(FrozenInstanceError):
        candidate.runtime_manifest_digest = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="invalid provider candidate metadata"):
        replace(candidate, runtime_manifest_digest="sha256:" + "0" * 64)
    with pytest.raises(provider.ProviderContractError):
        provider.ProviderProfile(
            profile_id=candidate.provider_profile.profile_id,
            model_id=candidate.model_id,
            availability=provider.Availability.AVAILABLE,
            qualification_id="0" * 64,
        )


@pytest.mark.parametrize(
    "build",
    [
        lambda: candidates.FilePin("/absolute", "0" * 64, 1),
        lambda: candidates.FilePin("./config.json", "0" * 64, 1),
        lambda: candidates.FilePin("a/../config.json", "0" * 64, 1),
        lambda: candidates.FilePin("a//config.json", "0" * 64, 1),
        lambda: candidates.FilePin("C:/config.json", "0" * 64, 1),
        lambda: candidates.FilePin("a\\config.json", "0" * 64, 1),
        lambda: candidates.FilePin("config.json", "0" * 63, 1),
        lambda: candidates.FilePin(1, "0" * 64, 1),
        lambda: replace(candidates.GLM_53_CANDIDATE.backend, release="nightly", backend_id="vllm-nightly"),
        lambda: replace(candidates.GLM_53_CANDIDATE.backend, source_revision=1),
        lambda: replace(candidates.GLM_53_CANDIDATE, unresolved_gates=("hardware_qualification",)),
        lambda: replace(candidates.GLM_53_CANDIDATE, unresolved_gates=(["unhashable"],)),
        lambda: replace(
            candidates.GLM_53_CANDIDATE,
            unresolved_gates=tuple(f"gate_{index:02d}" for index in range(33)),
        ),
        lambda: replace(candidates.GLM_53_CANDIDATE, model_id="zai-org/GLM-5.3-Flash"),
        lambda: replace(candidates.GLM_53_CANDIDATE, prompt_source_files=()),
        lambda: replace(
            candidates.GLM_53_CANDIDATE,
            prompt_source_files=(candidates.FilePin("encoding/encoding.py", "0" * 64, 1),),
        ),
    ],
)
def test_malformed_or_substituted_metadata_is_refused(build):
    with pytest.raises(ValueError, match="invalid provider candidate metadata"):
        build()
