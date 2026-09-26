#!/usr/bin/env python3
"""A stand-in for the Anthropic Messages API, for tests/install/convo_e2e.py only.

    fake_anthropic_api.py PORT TRACKER

Claude Code in the test container talks to it (ANTHROPIC_BASE_URL=http://127.0.0.1:PORT
and a made-up ANTHROPIC_API_KEY), so the real binary runs a real conversation with no
account and no network: every prompt gets the reply "ok", except a prompt that holds
`E2E-TASK <id>` — that one is answered with a Bash tool call running
`python3 TRACKER add-task <id> ... && python3 TRACKER set-task <id> done`, which
Claude Code executes in its own shell (the way an agent in a terminal adds a task to
the board); the tool result gets "done". `-v` as a third argument logs each request's
tool names and last message on stderr.
Streaming (SSE) and plain JSON replies; /v1/messages/count_tokens answers; anything
else is a 404 in the API's error shape. Binds 127.0.0.1 only.
"""
import json
import re
import shlex
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
TRACKER = sys.argv[2]
TASK = re.compile(r"E2E-TASK ([a-z0-9-]{1,40})")
_n = [0]
DEBUG = len(sys.argv) > 3 and sys.argv[3] == "-v"


def _text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            out.append(c.get("text") or "")
    return "\n".join(out)


def reply_for(body):
    """[content blocks], stop_reason for the request."""
    # Claude Code 2.1.282 puts "system" role messages (environment, hook feedback)
    # after the prompt: the turn is the last user/assistant message
    msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")]
    last = msgs[-1] if msgs else {}
    content = last.get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                         for c in content):
        return [{"type": "text", "text": "done"}], "end_turn"
    m = TASK.search(_text_of(content)) if last.get("role") == "user" else None
    tools = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
    if m and "Bash" in tools:
        _n[0] += 1
        # closed at once: AgentDeck's stop guard would otherwise (rightly) keep the
        # turn going while a task of this session is open on the board
        t = shlex.quote(TRACKER)
        cmd = (f"python3 {t} add-task {m.group(1)} --title 'added by Claude in the pane' "
               f"--agent claude && python3 {t} set-task {m.group(1)} done")
        return [{"type": "tool_use", "id": f"toolu_e2e{_n[0]:04d}", "name": "Bash",
                 "input": {"command": cmd, "description": "Add the task to the board"}}], "tool_use"
    return [{"type": "text", "text": "ok"}], "end_turn"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("fake-api: " + fmt % a + "\n")

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(404, {"type": "error", "error": {"type": "not_found_error",
                                                    "message": "fake api: not here"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        path = self.path.split("?", 1)[0]
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if path.endswith("/count_tokens"):
            return self._send(200, {"input_tokens": 12})
        if not path.endswith("/v1/messages"):
            return self.do_GET()
        blocks, stop = reply_for(body)
        if DEBUG:
            last = (body.get("messages") or [{}])[-1]
            sys.stderr.write("fake-api: tools=%s last=%s -> %s\n" % (
                sorted(t.get("name", "?") for t in body.get("tools") or [] if isinstance(t, dict))[:60],
                json.dumps(last)[:600], stop))
        _n[0] += 1
        msg = {"id": f"msg_e2e{_n[0]:04d}", "type": "message", "role": "assistant",
               "model": body.get("model") or "claude-e2e", "content": blocks,
               "stop_reason": stop, "stop_sequence": None,
               "usage": {"input_tokens": 12, "output_tokens": 3,
                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        if not body.get("stream"):
            return self._send(200, msg)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def ev(kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())

        start = dict(msg, content=[], stop_reason=None)
        start["usage"] = dict(msg["usage"], output_tokens=1)
        ev("message_start", {"type": "message_start", "message": start})
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "text_delta", "text": b["text"]}})
            else:
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": dict(b, input={})})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "input_json_delta",
                                                     "partial_json": json.dumps(b["input"])}})
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev("message_delta", {"type": "message_delta",
                             "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 3}})
        ev("message_stop", {"type": "message_stop"})
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
