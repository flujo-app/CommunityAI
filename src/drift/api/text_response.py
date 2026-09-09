"""OpenAI responses assembled from peer-produced text, without local tensors."""

import json
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from drift.text_mesh import TextPeerUnavailable


async def text_peer_response(loaded, body, *, chat, semaphore):
    request_id = ("chatcmpl-" if chat else "cmpl-") + uuid.uuid4().hex[:24]
    created = int(time.time())
    model_id = loaded.descriptor.model_id
    request = body.model_dump(exclude_none=True)
    request["model"] = loaded.descriptor.manifest_digest

    def chunk(text=None, *, done=None, role=False):
        choice = {"index": 0, "finish_reason": None if done is None else done.get("finish_reason", "stop")}
        if chat:
            choice["delta"] = {"role": "assistant", "content": ""} if role else ({"content": text} if text else {})
        else:
            choice["text"] = text or ""
        result = {
            "id": request_id,
            "object": "chat.completion.chunk" if chat else "text_completion",
            "created": created,
            "model": model_id,
            "choices": [choice],
        }
        if done is not None:
            result["usage"] = done["usage"]
        return "data: " + json.dumps(result) + "\n\n"

    async def events():
        iterator = loaded.runtime.text_client.stream(request, chat=chat)
        try:
            async with semaphore:
                async for frame in iterator:
                    yield frame
        finally:
            try:
                await iterator.aclose()
            finally:
                loaded.release()

    async def sse():
        frames = events()
        try:
            if chat:
                yield chunk(role=True)
            async for frame in frames:
                if frame["type"] == "delta":
                    yield chunk(frame["text"])
                elif frame["type"] == "done":
                    yield chunk(done=frame)
                else:
                    yield ": waiting for community\n\n"
            yield "data: [DONE]\n\n"
        except (TextPeerUnavailable, ValueError, TimeoutError) as exc:
            yield "data: " + json.dumps({"error": {"message": str(exc), "type": "server_error"}}) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            try:
                await frames.aclose()
            finally:
                # Also release if the caller disconnects after the role chunk,
                # before the peer event iterator has started.
                loaded.release()

    if body.stream:
        return StreamingResponse(sse(), media_type="text/event-stream")
    parts, done = [], None
    frames = events()
    try:
        async for frame in frames:
            if frame["type"] == "delta":
                parts.append(frame["text"])
            elif frame["type"] == "done":
                done = frame
        if done is None:
            raise TextPeerUnavailable("The community answer did not finish")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TextPeerUnavailable, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await frames.aclose()
    from drift.api.server import trim_stop_strings

    text = trim_stop_strings("".join(parts), body.stop)
    choice = {"index": 0, "finish_reason": done.get("finish_reason", "stop")}
    choice.update({"message": {"role": "assistant", "content": text}} if chat else {"text": text})
    return {
        "id": request_id,
        "object": "chat.completion" if chat else "text_completion",
        "created": created,
        "model": model_id,
        "choices": [choice],
        "usage": done["usage"],
    }
