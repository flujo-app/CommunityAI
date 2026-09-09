"""Input/output processing and generation owned by a contributing text peer."""

import asyncio
import logging
import queue
import threading
import time

from drift.api.server import (
    ChatCompletionRequest,
    CompletionRequest,
    _RequestCancelled,
    build_generate_kwargs,
    message_text,
)

logger = logging.getLogger(__name__)


class _PrefillCancelled(Exception):
    pass


class TextGenerationEngine:
    def __init__(self, runtime, *, max_context_tokens=2048, max_output_tokens=512, request_timeout=900):
        self.runtime = runtime
        self.max_context_tokens = max_context_tokens
        self.max_output_tokens = max_output_tokens
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._active = {}

    def cancel(self, request_id, remote_peer):
        with self._lock:
            cancel = self._active.get((remote_peer, request_id))
            if cancel is None:
                return False
            cancel.event.set()
            return True

    def close(self):
        with self._lock:
            for cancel in self._active.values():
                cancel.event.set()

    async def stream(self, payload, remote_peer):
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or len(request_id) != 32 or type(payload.get("chat")) is not bool:
            yield {"type": "error", "code": "invalid_request", "message": "Invalid text request"}
            return
        cancel = _RequestCancelled()
        key = remote_peer, request_id
        with self._lock:
            # A chat application may request an answer and a conversation title
            # together. Keep one generation running and at most two waiting.
            busy = key in self._active or len(self._active) >= 3
            if not busy:
                self._active[key] = cancel
        if busy:
            yield {"type": "error", "code": "busy", "message": "This community peer is busy"}
            return
        out = queue.Queue(maxsize=8)
        task = asyncio.create_task(asyncio.to_thread(self._run_generation, payload, key, cancel, out))
        deadline = time.monotonic() + self.request_timeout
        last_heartbeat = 0
        try:
            while time.monotonic() < deadline:
                try:
                    frame = await asyncio.to_thread(out.get, True, 1)
                except queue.Empty:
                    if task.done():
                        await task
                        return
                    if time.monotonic() - last_heartbeat >= 5:
                        yield {"type": "heartbeat"}
                        last_heartbeat = time.monotonic()
                    continue
                yield frame
                if frame["type"] in ("done", "error"):
                    return
            yield {"type": "error", "code": "timeout", "message": "The community answer took too long"}
        finally:
            cancel.event.set()
            # Admission remains occupied until the real generation thread exits.
            # A disconnect must not allow a second request onto a still-busy model.

    def _run_generation(self, payload, key, cancel, out):
        try:
            while not cancel.event.is_set():
                if self._generation_lock.acquire(timeout=0.1):
                    try:
                        if not cancel.event.is_set():
                            self._generate(payload, key, cancel, out)
                    finally:
                        self._generation_lock.release()
                    return
        finally:
            with self._lock:
                self._active.pop(key, None)

    def _generate(self, payload, key, cancel, out):
        import torch
        from transformers import StoppingCriteriaList, TextIteratorStreamer

        def put(frame):
            while not cancel.event.is_set():
                try:
                    out.put(frame, timeout=0.1)
                    return
                except queue.Full:
                    continue

        class Streamer(TextIteratorStreamer):
            def on_finalized_text(self, text, stream_end=False):
                if text:
                    put({"type": "delta", "text": text})

        try:
            body = (ChatCompletionRequest if payload["chat"] else CompletionRequest).model_validate(payload.get("body"))
            if body.n != 1:
                raise ValueError("Only one answer at a time is supported")
            max_tokens = body.max_tokens
            if payload["chat"] and max_tokens is None:
                max_tokens = body.max_completion_tokens
            max_tokens = min(128, self.max_output_tokens) if max_tokens is None else max_tokens
            if type(max_tokens) is not int or not 1 <= max_tokens <= self.max_output_tokens:
                raise ValueError(f"Choose between 1 and {self.max_output_tokens} output tokens")
            if body.temperature is not None and not 0 <= body.temperature <= 2:
                raise ValueError("Temperature must be between 0 and 2")
            if body.top_p is not None and not 0 < body.top_p <= 1:
                raise ValueError("top_p must be greater than 0 and at most 1")
            stops = [body.stop] if isinstance(body.stop, str) else body.stop or []
            if len(stops) > 16 or any(not s or len(s) > 128 for s in stops):
                raise ValueError("Stop sequences are too long or empty")
            tokenizer = self.runtime.tokenizer
            if payload["chat"]:
                if not body.messages or len(body.messages) > 128:
                    raise ValueError("Provide between 1 and 128 messages")
                messages = [{"role": m.role, "content": message_text(m.content)} for m in body.messages]
                options = {"enable_thinking": body.enable_thinking is True}
                input_ids = tokenizer.apply_chat_template(
                    messages, add_generation_prompt=True, return_dict=True, return_tensors="pt", **options
                )["input_ids"]
            else:
                prompt = body.prompt
                if isinstance(prompt, list):
                    if len(prompt) != 1:
                        raise ValueError("Batched prompts are not supported")
                    prompt = prompt[0]
                input_ids = tokenizer(prompt, return_tensors="pt").input_ids
            if input_ids.shape[1] + max_tokens > self.max_context_tokens:
                raise ValueError(f"This community peer supports {self.max_context_tokens} tokens including the answer")
            kwargs = build_generate_kwargs(
                max_tokens=max_tokens, temperature=body.temperature, top_p=body.top_p, stop=body.stop
            )
            # A Qwen 27B prompt with 533 tokens expands to more than 5 MiB
            # of activations. Prefill in small pieces to stay below the public
            # worker's 4 MiB message bound while retaining the entire prompt.
            kwargs["prefill_chunk_size"] = 64
            kwargs["stopping_criteria"] = StoppingCriteriaList([cancel])
            if stops:
                kwargs["tokenizer"] = tokenizer
            streamer = Streamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
            if cancel.event.is_set():
                return

            def check_cancel(_module, _inputs):
                if cancel.event.is_set():
                    raise _PrefillCancelled()

            # Transformers checks stopping criteria during decoding, but not
            # between prefill chunks. Stop there too after a caller disconnects.
            hook = self.runtime.model.register_forward_pre_hook(check_cancel)
            try:
                with torch.inference_mode():
                    output = self.runtime.model.generate(input_ids, streamer=streamer, **kwargs)
            finally:
                hook.remove()
            count = output.shape[1] - input_ids.shape[1]
            put(
                {
                    "type": "done",
                    "finish_reason": "length" if count >= max_tokens else "stop",
                    "usage": {
                        "prompt_tokens": input_ids.shape[1],
                        "completion_tokens": count,
                        "total_tokens": output.shape[1],
                    },
                }
            )
        except _PrefillCancelled:
            return
        except ValueError as exc:
            put({"type": "error", "code": "invalid_request", "message": str(exc)[:256]})
        except Exception:
            logger.exception("Text peer generation failed")
            put({"type": "error", "code": "unavailable", "message": "This community peer could not finish the answer"})
