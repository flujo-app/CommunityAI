"""Exact span resource claims from already verified, immutable model metadata.

No file, network or accelerator operations occur during span resolution. Metadata
verification belongs to placement_memory; child startup independently verifies the
selected artifact set and enforces its device ceiling.
"""

from functools import lru_cache

from drift.model_manifest import ManifestBlockArtifactPlan, ManifestError, select_manifest_block_artifacts
from drift.node.contribution_planner import MAX_AUTOMATIC_PLACEMENT_BLOCKS, PlacementResourcePlan


class PlacementSpanResolver:
    """Monotone device/disk feasibility with constant-time artifact range unions.

    Positive layer estimates and artifact-set union make every infeasible span's
    supersets infeasible under these same fixed budgets. A sparse OR table avoids
    rescanning the checkpoint index for every allocator probe. Its size is bounded
    by the model's 512 layers; only 128 distinct artifact unions are memoized.
    """

    def __init__(self, manifest, metadata, *, max_device_memory_bytes: int, max_artifact_bytes: int):
        for value in (max_device_memory_bytes, max_artifact_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**63 - 1:
                raise ValueError("placement resource budgets must be positive bounded byte counts")
        if metadata.manifest_digest != manifest.digest_id:
            raise ManifestError("Placement metadata does not match the manifested model")
        self._profile = metadata.memory_profile
        if (
            self._profile.num_blocks != manifest.model.num_blocks
            or not 0 < self._profile.num_blocks <= MAX_AUTOMATIC_PLACEMENT_BLOCKS
        ):
            raise ManifestError("Placement memory profile does not match the manifested block count")
        self.max_device_memory_bytes = max_device_memory_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.manifest_digest = manifest.digest_id
        self.cache_root = metadata.cache_root
        # This also validates every index entry and complete layer coverage. The
        # index is scanned only during construction, never in a planner probe.
        whole = select_manifest_block_artifacts(
            manifest,
            block_prefix=metadata.block_prefix,
            start_block=0,
            end_block=manifest.model.num_blocks,
            weight_map=metadata.weight_map,
        )
        self._artifacts = whole.artifacts
        bits_by_path = {artifact.path: 1 << index for index, artifact in enumerate(self._artifacts)}
        base = 0
        for artifact in self._artifacts:
            if artifact.role in ("config", "weight_index"):
                base |= bits_by_path[artifact.path]
        layer_bits = [base] * self._profile.num_blocks
        if metadata.weight_map is None:
            layer_bits = [(1 << len(self._artifacts)) - 1] * self._profile.num_blocks
        else:
            prefix = metadata.block_prefix + "."
            for parameter, path in metadata.weight_map.items():
                if not parameter.startswith(prefix):
                    continue
                index_text, separator, _ = parameter[len(prefix) :].partition(".")
                if not separator or not index_text.isascii() or not index_text.isdecimal():
                    continue
                index = int(index_text)
                if str(index) == index_text and 0 <= index < self._profile.num_blocks:
                    layer_bits[index] |= bits_by_path[path]
        table = [tuple(layer_bits)]
        width = 2
        while width <= len(layer_bits):
            prior = table[-1]
            half = width // 2
            table.append(tuple(prior[index] | prior[index + half] for index in range(len(layer_bits) - width + 1)))
            width *= 2
        self._table = tuple(table)

        @lru_cache(maxsize=128)
        def describe(bits):
            artifacts = tuple(artifact for index, artifact in enumerate(self._artifacts) if bits & (1 << index))
            # Digest and bytes depend only on this exact artifact union, not the
            # selected layer range. Child verification uses the same type.
            plan = ManifestBlockArtifactPlan(0, 1, artifacts)
            return artifacts, plan.artifact_bytes, plan.artifact_set_digest

        self._describe = describe

    def _range_bits(self, start: int, end: int) -> int:
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end <= self._profile.num_blocks
        ):
            raise ValueError("placement span must be a nonempty range within the verified model")
        level = (end - start).bit_length() - 1
        width = 1 << level
        return self._table[level][start] | self._table[level][end - width]

    def artifact_plan(self, start: int, end: int) -> ManifestBlockArtifactPlan:
        artifacts, _, _ = self._describe(self._range_bits(start, end))
        return ManifestBlockArtifactPlan(start, end, artifacts)

    def __call__(self, start: int, end: int):
        bits = self._range_bits(start, end)
        device_bytes = self._profile.estimate_span(start, end)
        if device_bytes > self.max_device_memory_bytes:
            return None
        _, artifact_bytes, artifact_digest = self._describe(bits)
        if artifact_bytes > self.max_artifact_bytes:
            return None
        return PlacementResourcePlan(start, end, artifact_bytes, artifact_digest, device_bytes)
