#!/usr/bin/env python3
"""Check all recent traces with overhead to see if they all have nested downstream calls."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

OUTPUT = "/tmp/overhead-analysis.txt"

now = datetime.now(timezone.utc)
start = now - timedelta(hours=4)

cmd = [
    "aws", "xray", "get-trace-summaries",
    "--start-time", start.strftime("%Y-%m-%dT%H:%M:%S"),
    "--end-time", now.strftime("%Y-%m-%dT%H:%M:%S"),
    "--region", "us-east-1",
    "--output", "json",
]
result = subprocess.run(cmd, capture_output=True, text=True)
data = json.loads(result.stdout)
summaries = data.get("TraceSummaries", [])

# Get traces with >2s overhead
high_overhead = [
    t for t in summaries
    if (t.get("Duration", 0) - t.get("ResponseTime", 0)) > 2.0
]
high_overhead.sort(key=lambda t: t.get("Duration", 0) - t.get("ResponseTime", 0), reverse=True)

lines = []


def out(s=""):
    lines.append(s)


out(f"Found {len(high_overhead)} traces with >2s overhead (out of {len(summaries)} total)")
out()

# Fetch details for each
trace_ids = [t["Id"] for t in high_overhead[:5]]
if trace_ids:
    # batch-get-traces needs trace IDs as space-separated args
    cmd2 = [
        "aws", "xray", "batch-get-traces",
        "--trace-ids",
    ] + trace_ids + [
        "--region", "us-east-1",
        "--output", "json",
    ]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    if result2.returncode != 0:
        out(f"ERROR fetching traces: {result2.stderr[:200]}")
        details = {"Traces": []}
    else:
        details = json.loads(result2.stdout)

    for trace in details.get("Traces", []):
        tid = trace["Id"]
        # Find matching summary
        summary = next((t for t in summaries if t["Id"] == tid), {})
        xray_dur = summary.get("Duration", 0)
        xray_resp = summary.get("ResponseTime", 0)
        xray_overhead = xray_dur - xray_resp

        seg = json.loads(trace["Segments"][0]["Document"])
        seg_start = seg.get("start_time", 0)
        seg_end = seg.get("end_time", 0)
        seg_dur = seg_end - seg_start

        # Look for downstream calls
        downstream_calls = []
        for sub in seg.get("subsegments", []):
            for nested in sub.get("subsegments", []):
                ns = nested.get("start_time", 0)
                ne = nested.get("end_time", 0)
                nd = ne - ns if ne and ns else 0
                if nd > 0.5:
                    downstream_calls.append({
                        "name": nested.get("name", "?"),
                        "start": ns,
                        "end": ne,
                        "duration": nd,
                    })

        out(f"Trace: {tid}")
        out(f"  X-Ray: dur={xray_dur:.1f}s resp={xray_resp:.1f}s overhead={xray_overhead:.1f}s")
        out(f"  Segment: {seg_dur:.1f}s")

        if downstream_calls:
            for dc in downstream_calls:
                gap = dc["start"] - seg_end
                out(f"  Downstream: {dc['name']} dur={dc['duration']:.1f}s (starts {gap:.1f}s after seg end)")
            total_app = max(dc["end"] for dc in downstream_calls) - seg_start
            cf_overhead = xray_dur - total_app
            out(f"  App total: {total_app:.1f}s, CloudFront overhead: {cf_overhead:.1f}s")
        else:
            cf_overhead = xray_dur - seg_dur
            out(f"  No downstream calls found. CF overhead: {cf_overhead:.1f}s")
        out()

out("=" * 60)
out("PATTERN SUMMARY")
out("=" * 60)
out()
out("If most traces show downstream calls, the 'overhead' is actually")
out("a 2nd Bedrock request on the same connection + CF VPC Origin latency.")
out("If traces have NO downstream calls but still have overhead,")
out("then the overhead is purely CloudFront VPC Origin connection time.")

with open(OUTPUT, "w") as f:
    f.write("\n".join(lines))

print(f"Written to {OUTPUT}")
# Print summary
for line in lines:
    print(line)
