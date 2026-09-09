# Gate Q3.8 FP8 loader checkpoint — 2026-09-03

## Scope

This is an implementation checkpoint, not Qwen3.8 release qualification. It binds the
first executable Qwen3.8 FP8 product-path work to pushed source
`de15d9cf21b946fd9b916ca6048bed9b188ef888` without claiming official artifact
verification, a real Qwen layer execution, a complete route, stock parity, recovery,
packaged acquisition, or consumer-GPU measurements.

## Exact candidate

- Official source: `Qwen/Qwen3.8-27B-FP8`
- Immutable revision: `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`
- Manifest: `manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json`
- Manifest digest:
  `sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`
- Declared inventory: 73 artifacts and 30,889,967,831 bytes
- Product execution profile: 64 text blocks, BF16 execution, eager attention, and
  explicit `fp8_dequant` source handling
- Stock reference: `Qwen/Qwen3.8-27B` at
  `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`

The official model card describes fine-grained FP8 quantization with 128-by-128 blocks;
the source config declares `quant_method=fp8`. Manifest/config validation now requires
that source declaration and the `fp8_dequant` runtime profile in both directions.

## Implemented boundary

- Preserve the outer checkpoint's quantization method through
  `AutoDistributedConfig`.
- Dequantize every fine-grained FP8 weight with its matching
  `weight_scale_inv` grid into the manifested BF16/FP16 execution dtype.
- Reject missing scales, orphan FP8 tensors, incompatible shapes, a prequantized source
  without an explicit compatible profile, and an `fp8_dequant` profile without an FP8
  source.
- Carry the exact quantization profile through server advertisement, block loading,
  memory accounting, throughput labeling, CLI resolution, and the local product-path
  qualification worker.
- Pin both the official FP8 candidate and BF16 stock-reference inventories.
- Exercise an actual Transformers Qwen3.5-family block through the production
  config-dispatch, FP8 dequantization, load, and forward path using a small synthetic
  checkpoint. This does not represent a downloaded Qwen3.8 layer.

## Verification

An independent tester verified the frozen 18-path Git-index snapshot:

- primary offline matrix: 158 passed;
- independent offline regression subset: 146 passed;
- Black, isort, Python compilation, and `git diff --cached --check`: passed;
- bidirectional source/profile validation, existing INT8/NF4 behavior, memory sizing,
  and worker advertisement/effective-profile agreement: passed.

A clean detached checkout of the pushed source ran:

`python scripts/qualify_model_manifest.py manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json --manifest-only --machine-id local-windows-metadata --source-commit de15d9cf21b946fd9b916ca6048bed9b188ef888`

The report passed manifest structure only and explicitly recorded
`complete_release_qualification=false`, `artifacts_verified=false`, no local parity,
and no failover.

## Real attempt and limits

An exploratory one-block CUDA product smoke first exposed that the qualification worker
hardcoded the unquantized profile. After the worker was corrected, the retry advanced to
official-source materialization and stopped at byte zero of `tokenizer.json` because the
Hugging Face CDN connection timed out. The attempt was not retained as qualification
evidence because it preceded the clean commit and did not verify the artifact inventory
or load a Qwen3.8 block.

The local Windows host is not representative release hardware and cannot hold the full
dequantized 64-block route. Read-only GCP preflight found native authentication healthy,
the single global GPU allowance unused, and only the protected bootstrap running. No
cloud resource, reservation, credit, or macOS work was used; checkpoint spend is USD 0.
The current USD 100 epoch still carries the prior conservative USD 56 Gate 13 maximum,
so no new paid run was authorized from the remaining USD 44.

## Next required outcome

Build and verify a source-bound split-worker execution plan that can acquire only each
worker's declared layer artifacts, then run the complete 64-block route, stock parity,
selected-worker interruption, same-session recovery, packaged cold acquisition/cache
reuse, and representative RTX 30/40/50 measurements under a separately recorded
authorization that keeps combined cloud exposure at or below USD 100.
