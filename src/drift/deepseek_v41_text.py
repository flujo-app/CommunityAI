"""Reviewed text-only subset of DeepSeek-V4.1-Flash's pinned chat encoder.

Reference: encoding/encoding.py at revision
dba1be0a40aa45a94ad051997016db3960a90277 (SHA-256
502bdaec8a3fd88ebc24c4721a7038fbe42f2063c664638127056107920035c1).
This implements only ``thinking_mode='chat'`` with an optional leading system
message and alternating plain-text user/assistant turns ending in a user turn.
It does not execute the downloaded reference or imply model qualification.
"""

from __future__ import annotations

_BOS = "<｜begin▁of▁sentence｜>"
_EOS = "<｜end▁of▁sentence｜>"
_SYSTEM = "<｜System｜>"
_USER = "<｜User｜>"
_ASSISTANT = "<｜Assistant｜>"
_CHAT = "</think>"
_MAX_MESSAGES = 65
_MAX_PROMPT_BYTES = 64 * 1024


def encode_deepseek_v41_text_chat(messages: list[dict]) -> str:
    """Encode the pinned non-thinking chat subset; refuse unsupported shapes."""
    if type(messages) is not list or not 1 <= len(messages) <= _MAX_MESSAGES:
        raise ValueError("DeepSeek chat requires 1..65 text messages")
    if type(messages[-1]) is not dict or messages[-1].get("role") != "user":
        raise ValueError("DeepSeek chat must end with a user turn")

    parts = [_BOS]
    content_bytes = 0
    next_role = "user"
    for index, message in enumerate(messages):
        if type(message) is not dict or set(message) != {"role", "content"}:
            raise ValueError("DeepSeek chat supports only role and text content")
        role, content = message["role"], message["content"]
        if (
            type(content) is not str
            or "｜" in content
            or any(marker in content for marker in ("<think", "</think", "<image", "<tool_result", "</tool_result"))
        ):
            raise ValueError("DeepSeek chat content contains an unsupported token or type")
        try:
            content_bytes += len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("DeepSeek chat content is not UTF-8") from exc
        if content_bytes > _MAX_PROMPT_BYTES:
            raise ValueError("DeepSeek chat prompt exceeds byte limit")
        if index == 0 and role == "system":
            parts.extend((_SYSTEM, content))
            continue
        if role != next_role:
            raise ValueError("DeepSeek chat turns must alternate user and assistant")
        if role == "user":
            parts.extend((_USER, content, _ASSISTANT, _CHAT))
            next_role = "assistant"
        else:
            parts.extend((content, _EOS))
            next_role = "user"

    prompt = "".join(parts)
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError("DeepSeek chat prompt exceeds byte limit")
    return prompt
