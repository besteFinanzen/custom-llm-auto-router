"""Scriptable stub upstream: OpenAI-compatible chat/completions + balance endpoints."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence request logging
        pass

    def _send(self, code: int, body, ctype: str = "application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/auth/key"):
            return self._send(200, {"data": {"limit_remaining": 5.0}})
        if self.path.endswith("/user/balance"):
            return self._send(200, {"is_available": True, "balance_infos": [
                {"currency": "USD", "total_balance": "2.50"}]})
        if self.path.endswith("/models"):
            ids = self.server.state.get("models", [])
            return self._send(200, {"object": "list",
                                    "data": [{"id": m, "object": "model"} for m in ids]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        model = body.get("model")
        self.server.state["received"].append(body)
        action = self.server.state["behavior"].get(model, "ok")
        if action == "429":
            return self._send(429, {"error": {"message": "rate limited", "type": "rate_limit_error"}})
        if action == "403":
            return self._send(403, {"error": {"message": "forbidden", "type": "auth_error"}})
        if action == "err":
            return self._send(500, {"error": {"message": "boom"}})
        if action == "sse":
            payload = (
                b'data: {"id":"s","object":"chat.completion.chunk","choices":[{"index":0,'
                b'"delta":{"role":"assistant","content":"hi"}}]}\n\n'
                b'data: {"id":"s","object":"chat.completion.chunk","choices":[{"index":0,'
                b'"delta":{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            )
            return self._send(200, payload, ctype="text/event-stream")
        return self._send(200, {
            "id": "c", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": f"hello from {model}"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })


class StubServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.httpd.state = {"behavior": {}, "received": []}
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()
