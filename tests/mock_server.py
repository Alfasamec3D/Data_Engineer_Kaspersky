"""Mock OpenAI endpoint (SSE) for checking loadtest.py without vLLM: python3 tests/mock_server.py 8999"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "test-key"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep quiet
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        n = body["max_tokens"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        time.sleep(0.05)  # simulated prefill
        for i in range(n):
            chunk = {"choices": [{"delta": {"content": f"t{i} "}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.005)
        usage = {"choices": [], "usage": {"completion_tokens": n}}
        self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
