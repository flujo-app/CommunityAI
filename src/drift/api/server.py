"""OpenAI-compatible HTTP API for a DRIFT-LLM swarm.

``drift api <model> --initial_peers ...`` starts a FastAPI app that joins the swarm as a regular
client (embeddings and lm_head run locally, transformer blocks run on the swarm) and exposes the
familiar OpenAI surface: ``/v1/models``, ``/v1/chat/completions`` and ``/v1/completions``,
including SSE streaming. Point any OpenAI SDK at ``http://host:port/v1`` and it works.

The app is deliberately a thin shim: requests are translated into ``model.generate(...)`` kwargs
and transformers' ``TextIteratorStreamer`` does the token-by-token work. Generation is gated by a
semaphore (``--max_concurrent``, default 1) because every in-flight request holds an inference
session and its server-side attention cache for the whole generation.

Known simplifications: ``n > 1``, logprobs, and tool calls are not supported; ``stop`` strings are
honored but may still appear in *streamed* output (the non-streaming path trims them).
"""

import asyncio
import json
import secrets
import time
import uuid
from queue import Empty
from typing import Any, Callable, Dict, List, Optional, Union

import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from hivemind.utils.logging import get_logger
from pydantic import BaseModel
from transformers import TextIteratorStreamer

from drift.node.model_manager import (
    AmbiguousModelError,
    LoadedModel,
    ModelManager,
    ModelManagerClosedError,
    ModelNotFoundError,
)

logger = get_logger(__name__)

DEFAULT_MAX_TOKENS = 512
# How long a single streamer poll may block its executor thread; on timeout we check whether the
# generation thread died (so a crashed generate() cannot hang the SSE response forever).
_STREAM_POLL_SECONDS = 5.0
_STREAM_DONE = object()


def _next_piece(streamer):
    """next(streamer), with StopIteration converted to a sentinel.

    A StopIteration escaping a run_in_executor callable is swallowed by the awaiting task's
    machinery (asyncio treats it as "the coroutine returned") and the await never resumes, so it
    must not cross the executor boundary as an exception. queue.Empty (the streamer's poll
    timeout) is an ordinary exception and propagates fine.
    """
    try:
        return next(streamer)
    except StopIteration:
        return _STREAM_DONE


class ChatMessage(BaseModel):
    role: str
    content: Any = ""


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    stream: bool = False
    n: int = 1


class CompletionRequest(BaseModel):
    model: Optional[str] = None
    prompt: Union[str, List[str]]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    stream: bool = False
    n: int = 1


def message_text(content: Any) -> str:
    """Flatten OpenAI message content (a string, or a list of typed parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def build_generate_kwargs(
    *,
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
    stop: Optional[Union[str, List[str]]],
    default_max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Dict[str, Any]:
    """Translate OpenAI sampling parameters into ``model.generate`` kwargs.

    OpenAI semantics: temperature defaults to 1.0 (sampling); temperature 0 means greedy.
    """
    kwargs: Dict[str, Any] = {"max_new_tokens": max_tokens if max_tokens is not None else default_max_tokens}
    if temperature is not None and temperature <= 0:
        kwargs["do_sample"] = False
    else:
        kwargs["do_sample"] = True
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
    if stop:
        kwargs["stop_strings"] = [stop] if isinstance(stop, str) else list(stop)
    return kwargs


def trim_stop_strings(text: str, stop: Optional[Union[str, List[str]]]) -> str:
    """transformers keeps the matched stop string in the output; OpenAI semantics exclude it."""
    if not stop:
        return text
    for stop_string in [stop] if isinstance(stop, str) else stop:
        index = text.find(stop_string)
        if index != -1:
            text = text[:index]
    return text


def create_app(
    model=None,
    tokenizer=None,
    model_name: Optional[str] = None,
    *,
    model_manager: Optional[ModelManager] = None,
    api_keys: Optional[List[str]] = None,
    api_key_verifier: Optional[Callable[[str], bool]] = None,
    max_concurrent: int = 1,
    default_max_tokens: int = DEFAULT_MAX_TOKENS,
) -> FastAPI:
    """Create the OpenAI-compatible application.

    ``model``/``tokenizer``/``model_name`` retain the original single-model API.
    The node daemon supplies ``model_manager`` instead, enabling exact alias
    resolution and lazy loading without changing the HTTP request surface.
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be at least 1")
    if default_max_tokens < 1:
        raise ValueError("default_max_tokens must be at least 1")
    if model_manager is None:
        if model is None or tokenizer is None or model_name is None:
            raise ValueError("model, tokenizer, and model_name are required without model_manager")
        model_manager = ModelManager()
        model_manager.register_loaded(model_name, model, tokenizer)
    elif model is not None or tokenizer is not None or model_name is not None:
        raise ValueError("pass either model_manager or the single-model arguments, not both")
    if api_keys and api_key_verifier is not None:
        raise ValueError("pass either api_keys or api_key_verifier, not both")

    app = FastAPI(title="DRIFT-LLM OpenAI-compatible API")
    semaphore = asyncio.Semaphore(max_concurrent)
    served_since = int(time.time())

    def check_auth(request: Request) -> None:
        if not api_keys and api_key_verifier is None:
            return
        auth = request.headers.get("authorization", "")
        candidate = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        valid = (
            api_key_verifier(candidate)
            if api_key_verifier is not None
            else any(secrets.compare_digest(candidate, key) for key in api_keys)
        )
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid API key")

    async def load_model(identifier: Optional[str]) -> LoadedModel:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, model_manager.load, identifier)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A thread waiting for a residency slot or downloading a model cannot be
            # cancelled through asyncio. If the HTTP request goes away, release the
            # eventual lease instead of permanently pinning the runtime.
            def release_abandoned_load(completed_future) -> None:
                try:
                    completed_future.result().release()
                except BaseException:
                    pass

            future.add_done_callback(release_abandoned_load)
            raise
        except AmbiguousModelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ModelNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ModelManagerClosedError as exc:
            raise HTTPException(status_code=503, detail="Node is shutting down") from exc
        except Exception as exc:
            logger.exception("Model %r failed to load", identifier)
            raise HTTPException(status_code=503, detail="Model is unavailable") from exc

    def generate_sync(
        loaded: LoadedModel, input_ids: torch.Tensor, gen_kwargs: Dict[str, Any], streamer=None
    ) -> torch.Tensor:
        # Transformers requires the tokenizer for stop-string matching, but otherwise
        # forwards unknown generate kwargs into model.forward(). Distributed model
        # forwards intentionally reject a stray ``tokenizer`` argument.
        call_kwargs = dict(gen_kwargs)
        if call_kwargs.get("stop_strings"):
            call_kwargs["tokenizer"] = loaded.runtime.tokenizer
        with torch.inference_mode():
            return loaded.runtime.model.generate(input_ids, streamer=streamer, **call_kwargs)

    def usage(input_ids: torch.Tensor, output_ids: torch.Tensor) -> Dict[str, int]:
        prompt_tokens = input_ids.shape[1]
        completion_tokens = output_ids.shape[1] - prompt_tokens
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def finish_reason(completion_tokens: int, gen_kwargs: Dict[str, Any]) -> str:
        return "length" if completion_tokens >= gen_kwargs["max_new_tokens"] else "stop"

    async def sse_stream(loaded: LoadedModel, input_ids: torch.Tensor, gen_kwargs: Dict[str, Any], *, chat: bool):
        """Yield OpenAI-format SSE chunks while generate() runs in a worker thread."""
        request_id = f"{'chatcmpl' if chat else 'cmpl'}-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        object_name = "chat.completion.chunk" if chat else "text_completion"
        selected_model = loaded.descriptor.model_id
        selected_tokenizer = loaded.runtime.tokenizer

        def chunk(payload: Dict[str, Any], reason: Optional[str] = None) -> str:
            choice: Dict[str, Any] = {"index": 0, "finish_reason": reason}
            choice.update(payload)
            body = {"id": request_id, "object": object_name, "created": created, "model": selected_model}
            body["choices"] = [choice]
            return f"data: {json.dumps(body)}\n\n"

        future = None
        release_deferred = False
        try:
            async with semaphore:
                loop = asyncio.get_running_loop()
                streamer = TextIteratorStreamer(
                    selected_tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=_STREAM_POLL_SECONDS
                )
                future = loop.run_in_executor(None, lambda: generate_sync(loaded, input_ids, gen_kwargs, streamer))
                if chat:
                    yield chunk({"delta": {"role": "assistant", "content": ""}})
                while True:
                    try:
                        piece = await loop.run_in_executor(None, _next_piece, streamer)
                    except Empty:
                        if future.done():
                            break  # generate() died before calling streamer.end(); surfaced below
                        continue
                    if piece is _STREAM_DONE:
                        break
                    if piece:
                        yield chunk({"delta": {"content": piece}} if chat else {"text": piece})
                try:
                    output_ids = await asyncio.shield(future)
                    reason = finish_reason(output_ids.shape[1] - input_ids.shape[1], gen_kwargs)
                except Exception as exc:
                    logger.exception("Generation failed mid-stream")
                    yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'server_error'}})}\n\n"
                    reason = "stop"
                yield chunk({"delta": {}} if chat else {"text": ""}, reason)
                yield "data: [DONE]\n\n"
        finally:
            # Cancelling an HTTP stream does not stop model.generate() in its executor
            # thread. Keep the residency lease until that thread really exits so an
            # unload or LRU eviction cannot close the runtime underneath generation.
            if future is not None and not future.done():
                future.add_done_callback(lambda _: loaded.release())
                release_deferred = True
            if not release_deferred:
                loaded.release()

    @app.get("/health")
    async def health():
        snapshots = model_manager.snapshots()
        response = {"status": "ok", "models": len(snapshots)}
        if len(snapshots) == 1:
            response["model"] = snapshots[0].model_id
        return response

    @app.get("/v1/models")
    async def list_models(request: Request):
        check_auth(request)
        return {
            "object": "list",
            "data": [
                {
                    "id": snapshot.model_id,
                    "object": "model",
                    "created": served_since,
                    "owned_by": "drift",
                    "status": snapshot.state.value,
                    "availability": snapshot.route.get("status") if snapshot.route is not None else "unknown",
                    "aliases": list(snapshot.aliases),
                    "manifest_digest": snapshot.manifest_digest,
                }
                for snapshot in model_manager.snapshots()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(body: ChatCompletionRequest, request: Request):
        check_auth(request)
        if body.n != 1:
            raise HTTPException(status_code=400, detail="n > 1 is not supported")
        loaded = await load_model(body.model)
        selected_model = loaded.descriptor.model_id
        selected_tokenizer = loaded.runtime.tokenizer
        try:
            messages = [{"role": m.role, "content": message_text(m.content)} for m in body.messages]
            input_ids = selected_tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, return_dict=True, return_tensors="pt"
            )["input_ids"]
            gen_kwargs = build_generate_kwargs(
                max_tokens=body.max_tokens if body.max_tokens is not None else body.max_completion_tokens,
                temperature=body.temperature,
                top_p=body.top_p,
                stop=body.stop,
                default_max_tokens=default_max_tokens,
            )
        except BaseException:
            loaded.release()
            raise

        if body.stream:
            return StreamingResponse(
                sse_stream(loaded, input_ids, gen_kwargs, chat=True), media_type="text/event-stream"
            )

        future = None
        release_deferred = False
        try:
            async with semaphore:
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(None, lambda: generate_sync(loaded, input_ids, gen_kwargs))
                output_ids = await asyncio.shield(future)
        finally:
            if future is not None and not future.done():
                future.add_done_callback(lambda _: loaded.release())
                release_deferred = True
            if not release_deferred:
                loaded.release()
        text = selected_tokenizer.decode(output_ids[0, input_ids.shape[1] :], skip_special_tokens=True)
        text = trim_stop_strings(text, body.stop)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": selected_model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason(output_ids.shape[1] - input_ids.shape[1], gen_kwargs),
                }
            ],
            "usage": usage(input_ids, output_ids),
        }

    @app.post("/v1/completions")
    async def completions(body: CompletionRequest, request: Request):
        check_auth(request)
        if body.n != 1:
            raise HTTPException(status_code=400, detail="n > 1 is not supported")
        loaded = await load_model(body.model)
        selected_model = loaded.descriptor.model_id
        selected_tokenizer = loaded.runtime.tokenizer
        try:
            if not isinstance(body.prompt, str):
                if len(body.prompt) != 1:
                    raise HTTPException(status_code=400, detail="Batched prompts are not supported")
                body.prompt = body.prompt[0]
            input_ids = selected_tokenizer(body.prompt, return_tensors="pt").input_ids
            gen_kwargs = build_generate_kwargs(
                max_tokens=body.max_tokens,
                temperature=body.temperature,
                top_p=body.top_p,
                stop=body.stop,
                default_max_tokens=default_max_tokens,
            )
        except BaseException:
            loaded.release()
            raise

        if body.stream:
            return StreamingResponse(
                sse_stream(loaded, input_ids, gen_kwargs, chat=False), media_type="text/event-stream"
            )

        future = None
        release_deferred = False
        try:
            async with semaphore:
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(None, lambda: generate_sync(loaded, input_ids, gen_kwargs))
                output_ids = await asyncio.shield(future)
        finally:
            if future is not None and not future.done():
                future.add_done_callback(lambda _: loaded.release())
                release_deferred = True
            if not release_deferred:
                loaded.release()
        text = selected_tokenizer.decode(output_ids[0, input_ids.shape[1] :], skip_special_tokens=True)
        text = trim_stop_strings(text, body.stop)
        return {
            "id": f"cmpl-{uuid.uuid4().hex[:24]}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": selected_model,
            "choices": [
                {
                    "index": 0,
                    "text": text,
                    "finish_reason": finish_reason(output_ids.shape[1] - input_ids.shape[1], gen_kwargs),
                }
            ],
            "usage": usage(input_ids, output_ids),
        }

    return app
