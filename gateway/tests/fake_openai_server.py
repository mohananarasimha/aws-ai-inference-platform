import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def chat_response(text=None, tool_calls=None, prompt_tokens=10, completion_tokens=5):
    """An OpenAI-compatible chat completion body, as vLLM returns it."""

    message = {"role": "assistant", "content": text}

    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": (
                        call["arguments"]
                        if isinstance(call["arguments"], str)
                        else json.dumps(call["arguments"])
                    )
                }
            }
            for call in tool_calls
        ]

    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop"
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
    }


class FakeOpenAIServer:
    """
    Local stand-in for vLLM's POST /v1/chat/completions.

    Replies are scripted with reply(); each request takes the next one.
    Every request body is kept in .requests for assertions.
    """

    def __init__(self):
        self.requests = []
        self._replies = []
        self._lock = threading.Lock()

        outer = self

        class Handler(BaseHTTPRequestHandler):

            def do_GET(self):
                self._respond("GET", {})

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                self._respond("POST", json.loads(self.rfile.read(length) or b"{}"))

            def _respond(self, method, body):

                with outer._lock:
                    outer.requests.append({
                        "method": method,
                        "path": self.path,
                        "body": body
                    })
                    reply = (
                        outer._replies.pop(0)
                        if outer._replies
                        else {"status": 500, "body": {"error": "no scripted reply"}}
                    )

                time.sleep(reply.get("delay", 0))

                payload = reply["body"]

                data = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode()
                )

                self.send_response(reply["status"])
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()

                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True
        )

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def reply(self, body, status=200, delay=0):
        with self._lock:
            self._replies.append({
                "body": body,
                "status": status,
                "delay": delay
            })

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
