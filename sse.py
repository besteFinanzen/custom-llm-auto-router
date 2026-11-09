"""Server-Sent Events normalization for OpenAI-compatible streams.

Some upstream providers keep sending ``delta`` chunks after a chunk that
already carries a ``finish_reason``. Strict OpenAI clients reject that. This
module holds the finish reason back, forwards the later chunks untouched, and
emits one synthetic final chunk (with an empty delta) just before ``[DONE]``.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

DONE = b"[DONE]"
_META_KEYS = ("id", "object", "created", "model", "system_fingerprint")


def _data_payload(line: bytes) -> bytes | None:
    """Return the payload of an SSE ``data:`` line, else ``None``."""
    if not line.startswith(b"data:"):
        return None
    return line[5:].strip()


def _synthetic_chunk(meta: dict, reasons: dict[int, str]) -> bytes:
    chunk = {key: meta[key] for key in _META_KEYS if key in meta}
    chunk.setdefault("id", "")
    chunk.setdefault("object", "chat.completion.chunk")
    chunk.setdefault("created", int(time.time()))
    chunk.setdefault("model", "")
    chunk["choices"] = [
        {"index": index, "delta": {}, "finish_reason": reason}
        for index, reason in reasons.items()
    ]
    return b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"


class StreamNormalizer:
    """Incrementally rewrite an upstream OpenAI SSE byte stream."""

    def __init__(self) -> None:
        self._buf = b""
        self._reasons: dict[int, str] = {}
        self._meta: dict = {}
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def feed(self, chunk: bytes) -> list[bytes]:
        """Consume upstream bytes and return complete downstream SSE frames."""
        if self._done:
            return []
        self._buf += chunk
        out: list[bytes] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.endswith(b"\r"):
                line = line[:-1]
            out.extend(self._line(line))
            if self._done:
                self._buf = b""
                break
        return out

    def finish(self) -> list[bytes]:
        """Flush at upstream EOF, including a final line without a newline."""
        if self._done:
            return []
        out: list[bytes] = []
        if self._buf:
            line = self._buf[:-1] if self._buf.endswith(b"\r") else self._buf
            self._buf = b""
            out.extend(self._line(line))
        out.extend(self._finish_frames(emit_done=False))
        return out

    def _line(self, line: bytes) -> list[bytes]:
        payload = _data_payload(line)
        if payload is None:
            return [line + b"\n"]
        if payload == DONE:
            return self._finish_frames(emit_done=True)
        try:
            obj = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return [line + b"\n"]
        if not isinstance(obj, dict):
            return [line + b"\n"]

        changed = False
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                reason = choice.get("finish_reason")
                if reason is None:
                    continue
                index = choice.get("index", 0)
                if not isinstance(index, int):
                    index = 0
                self._reasons.setdefault(index, reason)
                choice["finish_reason"] = None
                changed = True

        for key in _META_KEYS:
            if key in obj:
                self._meta[key] = obj[key]

        if changed:
            return [b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n"]
        return [line + b"\n"]

    def _finish_frames(self, *, emit_done: bool) -> list[bytes]:
        if self._done:
            return []
        self._done = True
        out: list[bytes] = []
        if self._reasons:
            out.append(_synthetic_chunk(self._meta, self._reasons))
            emit_done = True
        if emit_done:
            out.append(b"data: [DONE]\n\n")
        return out


async def normalize_sse_stream(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Yield a normalized SSE stream from raw upstream bytes."""
    normalizer = StreamNormalizer()
    async for chunk in source:
        for frame in normalizer.feed(chunk):
            yield frame
        if normalizer.done:
            return
    for frame in normalizer.finish():
        yield frame
