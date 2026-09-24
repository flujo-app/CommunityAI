"""Standalone GLM-5.3 text format prototype against pinned template renders.

The expected outputs were produced with a sandboxed Jinja render of the pinned
10734-byte chat_template.jinja. No model/tokenizer files or GPU are used.
"""

PREFIX = "[gMASK]<sop><|system|>Reasoning Effort: Max"


def encode(messages):
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("final user turn required")
    parts = [PREFIX]
    for message in messages:
        role, content = message["role"], message["content"]
        if role == "system":
            parts.extend(("<|system|>", content))
        elif role == "user":
            parts.extend(("<|user|>", content))
        elif role == "assistant":
            parts.extend(("<|assistant|><think></think>", content.strip()))
        else:
            raise ValueError("unsupported role")
    parts.append("<|assistant|><think>")
    return "".join(parts)


def main():
    assert encode([{"role": "user", "content": "hello"}]) == (PREFIX + "<|user|>hello<|assistant|><think>")
    assert encode([{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hello"}]) == (
        PREFIX + "<|system|>You are helpful.<|user|>hello<|assistant|><think>"
    )
    assert encode(
        [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
    ) == (PREFIX + "<|user|>q1<|assistant|><think></think>a1<|user|>q2<|assistant|><think>")
    print("GLM-5.3 text prototype: pinned template renders PASS")


if __name__ == "__main__":
    main()
