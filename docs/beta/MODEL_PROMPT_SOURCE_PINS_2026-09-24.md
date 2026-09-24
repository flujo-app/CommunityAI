# Exact-model prompt source pins

Status: **source metadata verified, bounded text encoders implemented**,
not a model load or CommunityAI model qualification. No model weights or
runtime image were fetched.

The immutable candidate revisions are DeepSeek-V4.1-Flash
`dba1be0a40aa45a94ad051997016db3960a90277` and GLM-5.3
`aca966e4e02791568aa6a4ced368624b3d897f42`. Their official Hub tree APIs
and raw small files were read on 2026-09-24. Each byte count and SHA-256 below
is now included in `UnavailableProviderCandidate` schema 2 and its canonical
profile digest:

Primary source paths: [DeepSeek encoding reference](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/tree/dba1be0a40aa45a94ad051997016db3960a90277/encoding)
and [GLM chat template](https://huggingface.co/zai-org/GLM-5.3/blob/aca966e4e02791568aa6a4ced368624b3d897f42/chat_template.jinja).

| Model | Pinned file | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| DeepSeek-V4.1-Flash | `encoding/README.md` | 12,120 | `a2f0fc3baea318c9cfbceca68cbfe50d37cf7da6605ace887f33148bcff7e3ae` |
| DeepSeek-V4.1-Flash | `encoding/encoding.py` | 37,316 | `502bdaec8a3fd88ebc24c4721a7038fbe42f2063c664638127056107920035c1` |
| DeepSeek-V4.1-Flash | `encoding/test_encoding.py` | 19,371 | `4a470892dad828459958cebfad55da598aafcb7a5878764ccbfc3ab060399d06` |
| GLM-5.3 | `chat_template.jinja` | 10,734 | `3740abcea51c45830cb3ca562084ad5fb2ef53589376f73332e9886f93ade41c` |

The pinned GLM tree contains a standalone `chat_template.jinja`; the earlier
research only found no embedded template in `tokenizer_config.json`. That
distinction is now recorded. DeepSeek's official repository provides an
encoding reference and explicitly does not rely on a generic Jinja template.
The pinned sources supply format evidence. A reviewed local encoder implements only
DeepSeek's `thinking_mode="chat"` for text-only alternating turns. It matches
the pinned reference's single-turn and leading-system test vectors; its
multi-turn vector follows the pinned renderer and prior-turn assertion. It
rejects tools, images, reasoning mode, reserved prompt tokens,
oversized prompts, and other unsupported shapes. It is wired to the managed
FastAPI chat route only when explicitly registered for a synthetic `test/*`
profile.

A separate GLM text encoder matches three sandboxed renders of the pinned
10,734-byte Jinja template: single-turn, leading-system, and multi-turn. It
uses the template's default Max reasoning effort and opens `<think>` for
generation; the managed synthetic profile requires the caller to explicitly
set `enable_thinking=true`. Unsupported tools, media, reserved tokens and
message shapes are refused. Neither encoder establishes tokenizer parity or
model quality on the exact weights. Real `prompt_encoder_revision`,
runtime/artifact digests, rights approval, and hardware qualification remain
unset, so both exact model profiles stay unavailable.

Tests verify exact paths and hashes, reject substituted prompt paths, and
prove that a changed source hash changes the profile digest. The focused
candidate suite passed **30 tests in 5.15 seconds** after formatting. No broad
CI or inference run was used.

The standalone prototypes and reviewed encoder checks passed in under a second
each. A local FastAPI-to-fake-vLLM check passed for synthetic text/chat and
confirmed exact-model refusal; the focused provider suite passed 46 tests in
5.58 seconds. No broad CI or inference run was used.

Next: bind an approved backend's actual prompt encoding and tokenizer to these
pins, verify wider reference parity for text inputs on the exact model, then
qualify reasoning separately. GLM's template alone does not establish the chosen
vLLM build's parser behavior or commercial eligibility; DeepSeek's Python
reference must not be executed as unreviewed downloaded code.
