#!/usr/bin/env python3
"""Inspect raw X-Ray trace structure to understand segment/subsegment layout."""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REGION = "us-east-1"

now = datetime.now(timezone.utc)
start = now - timedelta(hours=24)

# Get one trace
result = subprocess.run(
    [
        "aws", "xray", "get-trace-summaries",
        "--start-time", start.strftime("%Y-%m-%dT%H:%M:%S"),
        "--end-time", now.strftime("%Y-%m-%dT%H:%M:%S"),
        "--filter-expression", 'http.url CONTAINS "/model/" AND http.url CONTAINS "invoke"',
        "--region", REGION, "--output", "json",
    ],
    capture_output=True, text=True,
)
data = json.loads(result.stdout)
summaries = data.get("TraceSummaries", [])

if not summaries:
    print("No traces found")
    sys.exit(0)

# Pick the longest trace for inspection
summaries.sort(key=lambda x: x.get("Duration", 0), reverse=True)
trace_id = summaries[0]["Id"]
print(f"Inspecting trace: {trace_id} (duration={summaries[0]['Duration']:.1f}s)")

# Get full trace
result2 = subprocess.run(
    ["aws", "xray", "batch-get-traces", "--trace-ids", trace_id, "--region", REGION, "--output", "json"],
    capture_output=True, text=True,
)
trace_data = json.loads(result2.stdout)
traces = trace_data.get("Traces", [])

if not traces:
    print("No trace details found")
    sys.exit(0)

trace = traces[0]
print(f"\nSegments count: {len(trace.get('Segments', []))}")

for i, seg_wrapper in enumerate(trace["Segments"]):
    doc = json.loads(seg_wrapper.get("Document", "{}"))
    seg_name = doc.get("name", "?")
    seg_start = doc.get("start_time", 0)
    seg_end = doc.get("end_time", 0)
    seg_dur = seg_end - seg_start

    print(f"\n{'='*80}")
    print(f"Segment [{i}]: name={seg_name}  duration={seg_dur:.3f}s")
    print(f"  origin: {doc.get('origin', 'none')}")
    print(f"  http: {json.dumps(doc.get('http', {}), indent=4)[:500]}")
    print(f"  aws: {json.dumps(doc.get('aws', {}), indent=4)[:300]}")
    print(f"  annotations: {json.dumps(doc.get('annotations', {}), indent=4)[:300]}")
    print(f"  metadata: {list(doc.get('metadata', {}).keys())}")

    subsegments = doc.get("subsegments", [])
    print(f"  subsegments ({len(subsegments)}):")
    for j, sub in enumerate(subsegments):
        sub_name = sub.get("name", "?")
        sub_start = sub.get("start_time", 0)
        sub_end = sub.get("end_time", 0)
        sub_dur = sub_end - sub_start
        sub_ns = sub.get("namespace", "")
        print(f"    [{j}] name={sub_name}  duration={sub_dur:.3f}s  namespace={sub_ns}")

        # One more level
        subsubsegments = sub.get("subsegments", [])
        for k, subsub in enumerate(subsubsegments):
            subsub_name = subsub.get("name", "?")
            subsub_start = subsub.get("start_time", 0)
            subsub_end = subsub.get("end_time", 0)
            subsub_dur = subsub_end - subsub_start
            print(f"      [{k}] name={subsub_name}  duration={subsub_dur:.3f}s")

            # One more level
            for m, subsubsub in enumerate(subsub.get("subsegments", [])):
                sss_name = subsubsub.get("name", "?")
                sss_start = subsubsub.get("start_time", 0)
                sss_end = subsubsub.get("end_time", 0)
                sss_dur = sss_end - sss_start
                print(f"        [{m}] name={sss_name}  duration={sss_dur:.3f}s")
