#!/usr/bin/env python3
"""Analyze X-Ray traces to understand streaming vs non-streaming invoke patterns."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
start = now - timedelta(hours=6)

result = subprocess.run(
    [
        "aws", "xray", "get-trace-summaries",
        "--start-time", start.strftime("%Y-%m-%dT%H:%M:%S"),
        "--end-time", now.strftime("%Y-%m-%dT%H:%M:%S"),
        "--filter-expression", 'http.url CONTAINS "/model/" AND http.url CONTAINS "invoke"',
        "--region", "us-east-1",
        "--output", "json",
    ],
    capture_output=True, text=True,
)

if result.returncode != 0:
    print(f"AWS CLI error: {result.stderr}")
    exit(1)
if not result.stdout.strip():
    print(f"Empty response. stderr: {result.stderr}")
    exit(1)
data = json.loads(result.stdout)
summaries = data.get("TraceSummaries", [])

invoke_requests = []
stream_requests = []

for t in summaries:
    url = t.get("Http", {}).get("HttpURL", "")
    dur = t.get("Duration", 0)
    resp = t.get("ResponseTime", 0)
    status = t.get("Http", {}).get("HttpStatus", "?")

    # Extract model name
    model = "?"
    if "/model/" in url:
        model = url.split("/model/")[-1].split("/invoke")[0]

    entry = {"duration": dur, "response_time": resp, "overhead": dur - resp, "model": model, "status": status, "url": url}

    if "invoke-with-response-stream" in url:
        stream_requests.append(entry)
    elif "/invoke" in url:
        invoke_requests.append(entry)

print(f"=== Non-streaming /invoke: {len(invoke_requests)} requests ===")
if invoke_requests:
    durs = sorted([r["duration"] for r in invoke_requests])
    print(f"  avg={sum(durs)/len(durs):.1f}s  min={durs[0]:.1f}s  max={durs[-1]:.1f}s  p50={durs[len(durs)//2]:.1f}s")
    print()
    for r in sorted(invoke_requests, key=lambda x: x["duration"], reverse=True):
        model_short = r["model"].split(".")[-1][:35] if "." in r["model"] else r["model"][:35]
        print(f"  {r['duration']:>7.1f}s (resp={r['response_time']:.1f}s overhead={r['overhead']:.1f}s) [{r['status']}] {model_short}")

print(f"\n=== Streaming /invoke-with-response-stream: {len(stream_requests)} requests ===")
if stream_requests:
    durs = sorted([r["duration"] for r in stream_requests])
    print(f"  avg={sum(durs)/len(durs):.1f}s  min={durs[0]:.1f}s  max={durs[-1]:.1f}s  p50={durs[len(durs)//2]:.1f}s")
    print()
    for r in sorted(stream_requests, key=lambda x: x["duration"], reverse=True)[:20]:
        model_short = r["model"].split(".")[-1][:35] if "." in r["model"] else r["model"][:35]
        print(f"  {r['duration']:>7.1f}s (resp={r['response_time']:.1f}s overhead={r['overhead']:.1f}s) [{r['status']}] {model_short}")
    if len(stream_requests) > 20:
        print(f"  ... and {len(stream_requests) - 20} more")

# Summary
print(f"\n=== Summary ===")
total = len(invoke_requests) + len(stream_requests)
print(f"Total: {total} requests")
print(f"Non-streaming: {len(invoke_requests)} ({len(invoke_requests)/total*100:.0f}%)" if total else "")
print(f"Streaming: {len(stream_requests)} ({len(stream_requests)/total*100:.0f}%)" if total else "")

# Check which models use non-streaming
if invoke_requests:
    models = {}
    for r in invoke_requests:
        m = r["model"]
        if m not in models:
            models[m] = 0
        models[m] += 1
    print(f"\nModels using non-streaming /invoke:")
    for m, c in sorted(models.items(), key=lambda x: -x[1]):
        print(f"  {c:>3}x  {m}")
