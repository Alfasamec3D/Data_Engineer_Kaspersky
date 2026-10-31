#!/usr/bin/env python3
"""Load test for an OpenAI-compatible vLLM endpoint (stdlib only).

For every scenario (prompt size x concurrency) it measures:
  TTFT        - time to first token (first SSE chunk with content), p50 / p95
  Latency     - total request time, p50 / p95
  Throughput  - output tokens per second across all requests (output tok/s) and requests per second

Example:
  export API_KEY=...        # the same bearer token as in the Secret
  python3 loadtest/loadtest.py --url http://localhost:8000 --model qwen2.5-1.5b-instruct \
      --requests 32 --concurrency 1 4 8 --out loadtest/results/results.md
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

FILLER = (
    "The quick brown fox jumps over the lazy dog while data engineers move "
    "records between systems and monitor pipelines for latency and quality. "
)

# scenarios: name -> (approximate number of input words, max_tokens)
SCENARIOS = {
    "short": (16, 64),      # short prompt, short answer
    "long": (1500, 64),     # long context (~2k tokens), short answer
}


def make_prompt(words: int, nonce: str = "") -> str:
    # The unique nonce goes FIRST: vLLM caches prefixes by default, and identical prompts would
    # make every request after the first skip the prefill (TTFT of a "long" prompt would be fake).
    base = FILLER.split()
    out = (base * (words // len(base) + 1))[:words]
    return f"[{nonce}] " + " ".join(out) + "\n\nSummarize the text above in one sentence."


def one_request(url: urllib.parse.ParseResult, api_key: str, model: str, prompt: str,
                max_tokens: int, timeout: float) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    })
    conn_cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(url.hostname, url.port or (443 if url.scheme == "https" else 80), timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    t0 = time.perf_counter()
    ttft = None
    out_tokens = 0
    chunks = 0
    try:
        conn.request("POST", url.path.rstrip("/") + "/v1/chat/completions", body, headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return {"ok": False, "error": f"HTTP {resp.status}: {resp.read(200)!r}"}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            evt = json.loads(payload)
            if evt.get("usage"):
                out_tokens = evt["usage"].get("completion_tokens", out_tokens)
            for ch in evt.get("choices", []):
                if ch.get("delta", {}).get("content"):
                    chunks += 1
                    if ttft is None:
                        ttft = time.perf_counter() - t0
        latency = time.perf_counter() - t0
        if ttft is None:
            return {"ok": False, "error": "no content received"}
        return {"ok": True, "ttft": ttft, "latency": latency, "out_tokens": out_tokens or chunks}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}
    finally:
        conn.close()


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def run_scenario(url, api_key, model, name, words, max_tokens, n_requests, concurrency, timeout):
    prompts = [make_prompt(words, uuid.uuid4().hex) for _ in range(n_requests)]
    t0 = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(one_request, url, api_key, model, p, max_tokens, timeout)
                for p in prompts]
        for f in as_completed(futs):
            results.append(f.result())
    wall = time.perf_counter() - t0
    ok = [r for r in results if r["ok"]]
    errors = len(results) - len(ok)
    if not ok:
        return {"scenario": name, "concurrency": concurrency, "errors": errors,
                "first_error": next((r["error"] for r in results if not r["ok"]), "")}
    ttft = [r["ttft"] for r in ok]
    lat = [r["latency"] for r in ok]
    toks = sum(r["out_tokens"] for r in ok)
    return {
        "scenario": name, "concurrency": concurrency, "requests": n_requests, "errors": errors,
        "ttft_p50": statistics.median(ttft), "ttft_p95": pct(ttft, 0.95),
        "lat_p50": statistics.median(lat), "lat_p95": pct(lat, 0.95),
        "out_tok_s": toks / wall, "req_s": len(ok) / wall,
    }


def to_markdown(rows: list[dict]) -> str:
    head = ("| Prompt | Concurrency | Requests | Errors | TTFT p50, s | TTFT p95, s | "
            "Latency p50, s | Latency p95, s | Throughput, tok/s | req/s |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n")
    lines = []
    for r in rows:
        if "ttft_p50" not in r:
            lines.append(f"| {r['scenario']} | {r['concurrency']} | - | {r['errors']} | FAILED: {r.get('first_error','')} | | | | | |")
            continue
        lines.append(
            f"| {r['scenario']} | {r['concurrency']} | {r['requests']} | {r['errors']} | "
            f"{r['ttft_p50']:.2f} | {r['ttft_p95']:.2f} | {r['lat_p50']:.2f} | {r['lat_p95']:.2f} | "
            f"{r['out_tok_s']:.1f} | {r['req_s']:.2f} |")
    return head + "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--model", required=True, help="the SERVED_MODEL_NAME value")
    ap.add_argument("--api-key", default=os.environ.get("API_KEY", ""), help="or the API_KEY env var")
    ap.add_argument("--requests", type=int, default=32, help="requests per scenario")
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    ap.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--warmup", type=int, default=2, help="warm-up requests (not counted)")
    ap.add_argument("--out", help="save the markdown table to a file")
    args = ap.parse_args()

    url = urllib.parse.urlparse(args.url)
    for name in args.scenarios:
        words, max_tokens = SCENARIOS[name]
        for _ in range(args.warmup):
            one_request(url, args.api_key, args.model, make_prompt(words, uuid.uuid4().hex),
                        max_tokens, args.timeout)

    rows = []
    for name in args.scenarios:
        words, max_tokens = SCENARIOS[name]
        for c in args.concurrency:
            print(f"[run] scenario={name} concurrency={c} requests={args.requests}", file=sys.stderr)
            rows.append(run_scenario(url, args.api_key, args.model, name, words, max_tokens,
                                     args.requests, c, args.timeout))
    table = to_markdown(rows)
    print(table)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(table)
    return 1 if any("ttft_p50" not in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
