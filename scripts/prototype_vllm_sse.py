"""Tiny standalone vLLM-style streaming frame experiment (no vLLM needed)."""

import json


def frames(chunks, limit=1024):
    pending = b""
    for chunk in chunks:
        pending += chunk.replace(b"\r\n", b"\n")
        if len(pending) > limit:
            raise ValueError("frame too large")
        while b"\n\n" in pending:
            frame, pending = pending.split(b"\n\n", 1)
            lines = frame.split(b"\n")
            if len(lines) != 1 or not lines[0].startswith(b"data: "):
                raise ValueError("invalid SSE frame")
            data = lines[0][6:]
            yield data if data == b"[DONE]" else json.loads(data)
    if pending:
        raise ValueError("truncated SSE frame")


def main():
    body = b'data: {"model":"test/model","choices":[{"index":0,"text":"hello"}]}\n\n'
    assert list(frames([body[:13], body[13:], b"data: [DONE]\n\n"]))[0]["choices"][0]["text"] == "hello"
    try:
        list(frames([b"data: " + b"x" * 1024]))
        raise AssertionError("oversized frame accepted")
    except ValueError:
        pass
    print("vLLM SSE prototype: split frame and size limit PASS")


if __name__ == "__main__":
    main()
