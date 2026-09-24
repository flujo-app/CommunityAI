"""Reviewed text-only subset of GLM-5.3's pinned chat template.

Source: chat_template.jinja at revision
aca966e4e02791568aa6a4ced368624b3d897f42, SHA-256
3740abcea51c45830cb3ca562084ad5fb2ef53589376f73332e9886f93ade41c.
This matches the template's default Max reasoning effort, no tools or media,
and an assistant generation prompt. It does not qualify model execution.
"""

from __future__ import annotations

_PREFIX = "[gMASK]<sop><|system|>Reasoning Effort: Max"
_MAX_MESSAGES = 65
_MAX_PROMPT_BYTES = 64 * 1024


def encode_glm53_text_chat(messages: list[dict]) -> str:
    """Encode the pinned text-only thinking path; refuse other message shapes."""
    if type(messages) is not list or not 1 <= len(messages) <= _MAX_MESSAGES:
        raise ValueError("GLM chat requires 1..65 text messages")
    if type(messages[-1]) is not dict or messages[-1].get("role") != "user":
        raise ValueError("GLM chat must end with a user turn")

    parts = [_PREFIX]
    content_bytes = 0
    next_role = "user"
    for index, message in enumerate(messages):
        if type(message) is not dict or set(message) != {"role", "content"}:
            raise ValueError("GLM chat supports only role and text content")
        role, content = message["role"], message["content"]
        if type(content) is not str or any(
            marker in content for marker in ("<|", "[gMASK]", "<think", "</think", "<tool_", "</tool_")
        ):
            raise ValueError("GLM chat content contains an unsupported token or type")
        try:
            content_bytes += len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("GLM chat content is not UTF-8") from exc
        if content_bytes > _MAX_PROMPT_BYTES:
            raise ValueError("GLM chat prompt exceeds byte limit")
        if index == 0 and role == "system":
            parts.extend(("<|system|>", content))
            continue
        if role != next_role:
            raise ValueError("GLM chat turns must alternate user and assistant")
        if role == "user":
            parts.extend(("<|user|>", content))
            next_role = "assistant"
        else:
            parts.extend(("<|assistant|><think></think>", content.strip()))
            next_role = "user"

    parts.append("<|assistant|><think>")
    prompt = "".join(parts)
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError("GLM chat prompt exceeds byte limit")
    return prompt
