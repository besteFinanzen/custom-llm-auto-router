"""Unit tests for the SSE finish-reason normalizer."""
from __future__ import annotations

import json

from sse import normalize_sse_stream
from tests.conftest import run


def collect(chunks):
    async def go():
        async def source():
            for chunk in chunks:
                yield chunk
        return b"".join([frame async for frame in normalize_sse_stream(source())])
    return run(go())


def sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def chunk(delta: dict, finish_reason: str | None = None, *,
          index: int = 0, choices: list | None = None, **extra) -> dict:
    obj = {"id": "s", "object": "chat.completion.chunk", "created": 1,
           "model": "m", **extra}
    obj["choices"] = choices if choices is not None else [
        {"index": index, "delta": delta, "finish_reason": finish_reason}]
    return obj


def events(raw: bytes) -> list:
    out: list = []
    for line in raw.decode().splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        out.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return out


def test_finish_reason_deferred_until_done():
    raw = collect([
        sse(chunk({"role": "assistant", "content": "hi"})),
        sse(chunk({}, "stop")),
        sse(chunk({"content": "late"})),
        b"data: [DONE]\n\n",
    ])
    evs = events(raw)
    assert evs[0]["choices"][0]["delta"] == {"role": "assistant", "content": "hi"}
    assert evs[1]["choices"][0]["finish_reason"] is None
    assert evs[2]["choices"][0]["delta"] == {"content": "late"}
    assert evs[3]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    assert evs[4] == "[DONE]"
    assert raw.count(b'"finish_reason":"stop"') == 1


def test_stream_without_finish_reason_unchanged():
    raw = collect([
        sse(chunk({"content": "hi"})),
        b"data: [DONE]\n\n",
    ])
    assert events(raw) == [chunk({"content": "hi"}), "[DONE]"]


def test_natural_eof_flushes_synthetic_and_done():
    raw = collect([
        sse(chunk({}, "tool_calls")),
        sse(chunk({"content": "late"})),
    ])
    evs = events(raw)
    assert evs[0]["choices"][0]["finish_reason"] is None
    assert evs[1]["choices"][0]["delta"] == {"content": "late"}
    assert evs[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
    assert evs[3] == "[DONE]"


def test_json_split_across_network_chunks():
    payload = b"".join([
        sse(chunk({"content": "hi"})),
        sse(chunk({}, "stop")),
        b"data: [DONE]\n\n",
    ])
    raw = collect([payload[i:i + 1] for i in range(len(payload))])
    evs = events(raw)
    assert evs[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    assert evs[-1] == "[DONE]"


def test_crlf_stream_is_normalized():
    raw = collect([
        b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\r\n\r\n',
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    ])
    evs = events(raw)
    assert evs[0]["choices"][0]["delta"] == {"content": "hi"}
    assert evs[1]["choices"][0]["finish_reason"] is None
    assert evs[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    assert evs[3] == "[DONE]"
    assert b"\r" not in raw


def test_multiple_choices_get_their_own_finish_reason():
    raw = collect([
        sse(chunk({}, choices=[
            {"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"},
            {"index": 1, "delta": {"content": "b"}, "finish_reason": "tool_calls"},
        ])),
        b"data: [DONE]\n\n",
    ])
    evs = events(raw)
    assert evs[0]["choices"][0]["finish_reason"] is None
    assert evs[0]["choices"][1]["finish_reason"] is None
    assert evs[1]["choices"] == [
        {"index": 0, "delta": {}, "finish_reason": "stop"},
        {"index": 1, "delta": {}, "finish_reason": "tool_calls"},
    ]
    assert evs[2] == "[DONE]"


def test_first_finish_reason_for_an_index_wins():
    raw = collect([
        sse(chunk({}, "stop")),
        sse(chunk({}, "tool_calls")),
        b"data: [DONE]\n\n",
    ])
    evs = events(raw)
    assert evs[0]["choices"][0]["finish_reason"] is None
    assert evs[1]["choices"][0]["finish_reason"] is None
    assert evs[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]


def test_malformed_json_and_non_data_lines_pass_through():
    payload = b"data: {oops\n\nevent: ping\n: keepalive\n\n"
    assert collect([payload]) == payload


def test_data_after_done_is_ignored():
    raw = collect([
        sse(chunk({}, "stop")),
        b"data: [DONE]\n\n",
        sse(chunk({"content": "too late"})),
    ])
    evs = events(raw)
    assert evs[-1] == "[DONE]"
    assert all(e == "[DONE]" or e["choices"][0]["delta"] != {"content": "too late"}
               for e in evs)


def test_usage_only_chunk_is_forwarded_before_synthetic_finale():
    raw = collect([
        sse(chunk({}, "stop")),
        sse(chunk({}, choices=[], usage={"prompt_tokens": 3, "completion_tokens": 4})),
        b"data: [DONE]\n\n",
    ])
    evs = events(raw)
    assert evs[1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 4}
    assert evs[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    assert evs[3] == "[DONE]"
