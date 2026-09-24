"""Standalone text-only DeepSeek V4.1 chat-format experiment.

The first two vectors come directly from pinned vendor tests. The multi-turn
vector follows the pinned renderer and its previous-turn assertion. The
downloaded reference is never imported or executed.
"""

BOS = "<｜begin▁of▁sentence｜>"
EOS = "<｜end▁of▁sentence｜>"
USER = "<｜User｜>"
ASSISTANT = "<｜Assistant｜>"
SYSTEM = "<｜System｜>"


def encode(messages):
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("a final user turn is required")
    parts = [BOS]
    for message in messages:
        role, content = message["role"], message["content"]
        if role == "system":
            parts.extend((SYSTEM, content))
        elif role == "user":
            parts.extend((USER, content, ASSISTANT, "</think>"))
        elif role == "assistant":
            parts.extend((content, EOS))
        else:
            raise ValueError("unsupported role")
    return "".join(parts)


def main():
    assert encode([{"role": "user", "content": "hello"}]) == BOS + USER + "hello" + ASSISTANT + "</think>"
    assert encode(
        [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "hello"}]
    ) == (BOS + SYSTEM + "You are a helpful assistant." + USER + "hello" + ASSISTANT + "</think>")
    assert encode(
        [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
    ) == (BOS + USER + "q1" + ASSISTANT + "</think>" + "a1" + EOS + USER + "q2" + ASSISTANT + "</think>")
    print("DeepSeek V4.1 text-only prototype: pinned single-turn and multi-turn vectors PASS")


if __name__ == "__main__":
    main()
