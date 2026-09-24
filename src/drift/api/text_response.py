"""OpenAI responses assembled from peer-produced text, without local tensors."""

import asyncio
import json
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from drift.text_mesh import TextPeerUnavailable
from drift.text_request import RequestContext, RequestDeadlineExceeded, retain_request_task


async def text_peer_response(loaded, body, *, chat, semaphore, context=None):
    context = RequestContext.start(900.0) if context is None else context
    if type(context) is not RequestContext:
        loaded.release()
        raise ValueError("Invalid request context")
    request_id = ("chatcmpl-" if chat else "cmpl-") + uuid.uuid4().hex[:24]
    created = int(time.time())
    model_id = loaded.descriptor.model_id
    request = body.model_dump(exclude_none=True)
    request["model"] = loaded.descriptor.manifest_digest
    events_started = False

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
        nonlocal events_started
        events_started = True
        iterator, pending, acquired = None, None, False

        async def close_and_release():
            try:
                if pending is not None:
                    try:
                        await pending
                    except BaseException:
                        pass
                if iterator is not None:
                    await iterator.aclose()
            finally:
                if acquired:
                    semaphore.release()
                loaded.release()

        try:
            await context.acquire(semaphore)
            acquired = True
            context.require_live()
            client = loaded.runtime.text_client
            if getattr(client, "supports_request_context", False) is True:
                iterator = client.stream(request, chat=chat, context=context)
            else:
                iterator = client.stream(request, chat=chat)
            while True:
                context.require_live()
                pending = asyncio.create_task(anext(iterator))
                try:
                    frame = await context.run(pending)
                except StopAsyncIteration:
                    pending = None
                    break
                pending = None
                yield frame
                if frame.get("type") == "done":
                    # Terminal acceptance precedes cleanup. Do not apply the
                    # execution deadline to a further read/unwind after success.
                    return
        finally:
            # Cleanup has a separate observation grace, never an extension of
            # execution. Keep the actual lease/semaphore until cleanup finishes,
            # even if an ill-behaved iterator outlives this response.
            cleanup = retain_request_task(asyncio.create_task(close_and_release()))
            try:
                await asyncio.wait({cleanup}, timeout=3.0)
            except asyncio.CancelledError:
                raise

    async def sse():
        frames = events()
        done = False
        try:
            context.require_live()
            if chat:
                yield chunk(role=True)
            async for frame in frames:
                if frame["type"] == "delta":
                    yield chunk(frame["text"])
                elif frame["type"] == "done":
                    done = True
                    yield chunk(done=frame)
                else:
                    yield ": waiting for community\n\n"
            if not done:
                raise TextPeerUnavailable("The community answer did not finish")
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
                if not events_started:
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
    except RequestDeadlineExceeded:
        raise HTTPException(status_code=504, detail="Request deadline exceeded") from None
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
