"""Mock LiteLLM gateway (stdlib-only) — local evidence harness.

Stands in for the real LiteLLM proxy when no Foundry credentials are
available. Serves BOTH faces of the gateway contract:

  Admin face (backend -> gateway):
    POST /key/generate      -> mints a virtual key
    POST /key/delete        -> deletes keys
    GET  /key/info?key=...  -> spend readback (always 0.0)
    GET  /health/liveliness -> liveness

  OpenAI-compatible face (worker engine -> gateway):
    POST /chat/completions and /v1/chat/completions
      Non-streaming and SSE-streaming replies. The reply is a fixed,
      identifiable canned answer so the live feed PROVES which stack
      served it. A request whose last user message contains the word
      "tools" gets a single `code_search` tool call first, then a text
      reply on the follow-up — enough to exercise the tools node live.

Run (host):   python scripts/mock_gateway.py --port 4000
Run (docker): docker run --network collegium_thread --network-alias gateway \
                -p 4099:4000 -v "$PWD/scripts/mock_gateway.py:/app/mock_gateway.py:ro" \
                python:3.12-slim python /app/mock_gateway.py
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANSWER = (
    "I am the CUSTOM LangGraph engine answering live through the mock gateway. "
    "The ask-mode read-only tool surface is bound, the Postgres checkpointer is "
    "holding my state, and every step you see in this feed was emitted as a "
    "StepEvent from worker.engine.runner — not the Claude Agent SDK."
)

KEYS: dict[str, dict] = {}


def _chunk(model: str, delta: dict, finish: str | None = None) -> str:
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _usage_chunk(model: str, prompt_tokens: int, completion_tokens: int) -> str:
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return f"data: {json.dumps(payload)}\n\n"


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def _has_tool_result(body: dict) -> bool:
    return any(m.get("role") == "tool" for m in body.get("messages", []))


def _wanted_tool(body: dict) -> tuple[str, dict] | None:
    """Pick a canned tool call from the engine's REAL roster (RG validation).

    file_read  -> read-only investigation prompts (ask mode surface)
    file_write -> mutating prompts (drives the approval gate interrupt)
    """
    if not body.get("tools") or _has_tool_result(body):
        return None
    text = _last_user_text(body).lower()
    available = {t.get("function", {}).get("name") for t in body.get("tools", [])}
    if any(k in text for k in ("append", "edit", "write", "modify")) and "file_write" in available:
        return "file_write", {"file_path": "README.md",
                              "content": "# stamped workspace\n# engine gate probe\n"}
    if any(k in text for k in ("tools", "find the code", "read it", "investigation",
                               "deep read-only")) and "file_read" in available:
        return "file_read", {"file_path": "README.md"}
    return None


def _reply_text(body: dict) -> str:
    """The canned answer; echo the nudge canary when present (g check)."""
    all_text = json.dumps(body.get("messages", []))
    if "PANGOLIN" in all_text:
        return CANSWER + " Steering nudge acknowledged: PANGOLIN."
    if body.get("response_format", {}).get("type") == "json_schema":
        return json.dumps({"goal": "mock plan", "steps": [{"step": 1, "action": "mock"}]})
    return CANSWER


def _reply_plan(body: dict) -> tuple[list[dict], str | None]:
    """(deltas, finish): what to stream for this request."""
    wanted = _wanted_tool(body)
    if wanted:
        name, args = wanted
        return [
            {"role": "assistant", "content": ""},
            {"tool_calls": [{
                "index": 0,
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]},
        ], "tool_calls"
    return [{"role": "assistant", "content": _reply_text(body)}], "stop"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        pass

    # ------------------------------------------------------------- helpers
    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    # --------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._json(200, {"status": "ok"})
            return
        if self.path.startswith("/key/info"):
            self._json(200, {"info": {"spend": 0.0}})
            return
        self._json(404, {"error": "unknown path", "path": self.path})

    # -------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/key/generate":
            body = self._body()
            key = f"sk-mock-{uuid.uuid4().hex[:24]}"
            KEYS[key] = body
            self._json(200, {"key": key, "key_alias": body.get("key_alias", ""),
                             "max_budget": body.get("max_budget", 0)})
            return
        if self.path == "/key/delete":
            for key in self._body().get("keys", []):
                KEYS.pop(key, None)
            self._json(200, {"deleted": True})
            return
        if self.path in ("/chat/completions", "/v1/chat/completions"):
            self._chat(self._body())
            return
        self._json(404, {"error": "unknown path", "path": self.path})

    # -------------------------------------------------------------- chat
    def _chat(self, body: dict) -> None:
        model = body.get("model", "kimi-k2.6")
        deltas, finish = _reply_plan(body)
        prompt_tokens = sum(len(str(m.get("content", ""))) // 4
                            for m in body.get("messages", [])) or 1
        completion_tokens = len(CANSWER) // 4

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            for delta in deltas:
                self.wfile.write(_chunk(model, delta).encode())
                self.wfile.flush()
                time.sleep(0.05)  # visible streaming in the console feed
            self.wfile.write(_chunk(model, {}, finish).encode())
            self.wfile.write(_usage_chunk(model, prompt_tokens, completion_tokens).encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        message: dict = {"role": "assistant", "content": ""}
        for delta in deltas:
            if delta.get("content"):
                message["content"] += delta["content"]
            if delta.get("tool_calls"):
                message["tool_calls"] = delta["tool_calls"]
        self._json(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens,
                      "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens},
        })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[mock-gateway] listening on :{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
