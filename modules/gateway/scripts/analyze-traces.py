#!/usr/bin/env python3
"""Analyze X-Ray trace summaries to understand latency distribution."""
import json

with open("/tmp/traces-summary.json") as f:
    data = json.load(f)

summaries = data.get("TraceSummaries", [])

print(f"Total model traces: {len(summaries)}\n")
print(f"{'Duration':>10} {'RespTime':>10} {'Overhead':>10} {'Status':>6}  {'URL'}")
print("-" * 100)

durations = []
overheads = []

for t in sorted(summaries, key=lambda x: x.get("Duration", 0)):
    http = t.get("Http", {})
    dur = t.get("Duration", 0)
    resp_time = t.get("ResponseTime", 0)
    overhead = dur - resp_time
    url = http.get("HttpURL", "?")
    
    # Shorten URL
    if "/model/" in url:
        parts = url.split("/model/")[1]
        model = parts.split("/")[0][:35]
        endpoint = parts.split("/")[-1] if "/" in parts else ""
        short_url = f"{model}/{endpoint}"
    else:
        short_url = url[:50]
    
    status = http.get("HttpStatus", "?")
    print(f"{dur:>10.3f}s {resp_time:>10.3f}s {overhead:>10.3f}s {status:>6}  {short_url}")
    
    durations.append(dur)
    overheads.append(overhead)

if durations:
    durations.sort()
    overheads.sort()
    print(f"\n{'='*60}")
    print(f"Duration:  avg={sum(durations)/len(durations):.3f}s  p50={durations[len(durations)//2]:.3f}s  max={durations[-1]:.3f}s")
    print(f"Overhead:  avg={sum(overheads)/len(overheads):.3f}s  p50={overheads[len(overheads)//2]:.3f}s  max={overheads[-1]:.3f}s")
    print(f"(Overhead = Duration - ResponseTime, i.e. time outside the origin response)")
