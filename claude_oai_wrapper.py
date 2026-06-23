#!/usr/bin/env python3
"""OpenAI -> Claude Code wrapper.

Exposes an OpenAI-compatible /v1/chat/completions endpoint and serves it via the
Anthropic Messages API using the Claude Code OAuth credential + Claude Code
identity headers (the path hermes' anthropic_adapter already uses and which is
proven to authenticate against the Max subscription without the OpenAI-compat
"extra usage" gate). Aina points at this as a plain `custom` OpenAI provider while
aina-llm-std is down for hardware.

Upstream calls are non-streaming; stream=true is re-emitted as OpenAI SSE chunks
(single content/tool-call delta then [DONE]) — functional and simple. Haiku is
fast enough that one-shot latency is fine for a stopgap.

Runs in the hermes pod with the hermes venv (anthropic SDK) + the mounted
/data/.claude OAuth credential. Bind 127.0.0.1 only — same-pod consumers only.
"""
import json
import os
import sys
import time
import uuid
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/hermes")
from agent.anthropic_adapter import resolve_anthropic_token, build_anthropic_client  # noqa: E402

HOST = os.environ.get("CLAUDE_OAI_WRAPPER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAUDE_OAI_WRAPPER_PORT", "8787"))
DEFAULT_MODEL = os.environ.get("CLAUDE_OAI_WRAPPER_MODEL", "claude-haiku-4-5-20251001")


def _client():
    tok = resolve_anthropic_token()
    if not tok:
        raise RuntimeError("no anthropic OAuth token resolved (~/.claude/.credentials.json)")
    return build_anthropic_client(tok, None, timeout=900)


def _oai_to_anthropic(body):
    system_parts, messages = [], []
    for m in body.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts += [p.get("text", "") for p in content if p.get("type") == "text"]
        elif role == "tool":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id"),
                "content": content if isinstance(content, str) else json.dumps(content),
            }]})
        elif role == "assistant":
            blocks = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                blocks += [{"type": "text", "text": p.get("text", "")}
                           for p in content if p.get("type") == "text"]
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"), "input": args})
            messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:  # user (or unknown -> user)
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
            elif isinstance(content, list):
                blocks = []
                for p in content:
                    if p.get("type") == "text":
                        blocks.append({"type": "text", "text": p.get("text", "")})
                    elif p.get("type") == "image_url":
                        url = (p.get("image_url") or {}).get("url", "")
                        if url.startswith("data:"):
                            media, _, b64 = url[5:].partition(";base64,")
                            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
                        elif url:
                            blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                messages.append({"role": "user", "content": blocks or [{"type": "text", "text": ""}]})
            else:
                messages.append({"role": "user", "content": ""})

    params = {
        "model": body.get("model") or DEFAULT_MODEL,
        "max_tokens": int(body.get("max_tokens") or 4096),
        "messages": messages,
    }
    if system_parts:
        params["system"] = "\n\n".join(s for s in system_parts if s)
    tools = []
    for t in body.get("tools") or []:
        if t.get("type") == "function" and t.get("function"):
            fn = t["function"]
            tools.append({"name": fn["name"], "description": fn.get("description", ""),
                          "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    if tools:
        params["tools"] = tools
    tc = body.get("tool_choice")
    if tc in ("auto", None) and tools:
        params["tool_choice"] = {"type": "auto"}
    elif tc in ("required", "any"):
        params["tool_choice"] = {"type": "any"}
    elif isinstance(tc, dict) and tc.get("type") == "function":
        params["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
    if body.get("temperature") is not None:
        params["temperature"] = body["temperature"]
    if body.get("stop"):
        params["stop_sequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    return params


_FINISH = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls", "stop_sequence": "stop"}


def _anthropic_to_oai(resp, model):
    text = "".join(b.text for b in resp.content if b.type == "text")
    tool_calls = [{"id": b.id, "type": "function",
                   "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                  for b in resp.content if b.type == "tool_use"]
    msg = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": _FINISH.get(resp.stop_reason, "stop")}],
        "usage": {"prompt_tokens": resp.usage.input_tokens, "completion_tokens": resp.usage.output_tokens,
                  "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens},
    }


def _sse(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def _stream_from_completion(comp):
    cid, model, created = comp["id"], comp["model"], comp["created"]
    ch = comp["choices"][0]
    msg = ch["message"]

    def chunk(delta, finish=None):
        return _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
    yield chunk({"role": "assistant"})
    if msg.get("content"):
        yield chunk({"content": msg["content"]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        yield chunk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                     "function": {"name": tc["function"]["name"],
                                                  "arguments": tc["function"]["arguments"]}}]})
    yield chunk({}, finish=ch["finish_reason"])
    yield b"data: [DONE]\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz"):
            return self._send_json(200, {"status": "ok"})
        if self.path.rstrip("/").endswith("/models"):
            return self._send_json(200, {"object": "list", "data": [
                {"id": DEFAULT_MODEL, "object": "model", "owned_by": "anthropic"}]})
        return self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send_json(404, {"error": {"message": "not found"}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send_json(400, {"error": {"message": "bad request: %s" % e}})
        stream = bool(body.get("stream"))
        try:
            params = _oai_to_anthropic(body)
            resp = _client().messages.create(**params)
            comp = _anthropic_to_oai(resp, body.get("model") or DEFAULT_MODEL)
        except Exception as e:
            msg = getattr(e, "message", None) or str(e)
            sys.stderr.write("wrapper error: %s\n%s\n" % (msg, traceback.format_exc()))
            sys.stderr.flush()
            code = getattr(e, "status_code", 500) or 500
            err = {"error": {"message": msg, "type": "upstream_error"}}
            if stream:
                self.send_response(code)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(_sse(err))
                self.wfile.write(b"data: [DONE]\n\n")
                return
            return self._send_json(code, err)

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            for c in _stream_from_completion(comp):
                self.wfile.write(c)
            return
        return self._send_json(200, comp)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("claude_oai_wrapper listening on %s:%d model=%s\n" % (HOST, PORT, DEFAULT_MODEL))
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
