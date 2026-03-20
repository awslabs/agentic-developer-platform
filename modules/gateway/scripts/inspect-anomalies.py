#!/usr/bin/env python3
"""Deep-inspect the two anomalous traces:
1. 39.2s streaming with 21.5s stream delivery after TTFB
2. 27.1s non-streaming with 20.3s gateway overhead
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

REGION = "us-east-1"

TRACE_IDS = [
    "1-69a0df76-a769b88b10b64223fb0694df",  # 39.2s streaming
    "1-69a0e19c-c08ee51c311a48e4fca31df8",  # 27.1s non-streaming
]


def aws_cli(args):
    result = subprocess.run(
        ["aws"] + args + ["--region", REGION, "--output", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


data = aws_cli(["xray", "batch-get-traces", "--trace-ids"] + TRACE_IDS)

for trace in data.get("Traces", []):
    tid = trace["Id"]
    print(f"\n{'='*100}")
    print(f"TRACE: {tid}")

    for seg_wrapper in trace.get("Segments", []):
        doc = json.loads(seg_wrapper.get("Document", "{}"))
        seg_start = doc.get("start_time", 0)
        seg_end = doc.get("end_time", 0)
        seg_dur = seg_end - seg_start

        url = doc.get("http", {}).get("request", {}).get("url", "")
        print(f"\nSegment: {doc.get('name')}  duration={seg_dur:.3f}s")
        print(f"  URL: {url}")
        print(f"  Start: {datetime.fromtimestamp(seg_start, tz=timezone.utc).strftime('%H:%M:%S.%f')}")
        print(f"  End:   {datetime.fromtimestamp(seg_end, tz=timezone.utc).strftime('%H:%M:%S.%f')}")

        subsegments = doc.get("subsegments", [])

        # Find significant subsegments (duration > 0.01s)
        significant = []
        for sub in subsegments:
            sub_start = sub.get("start_time", 0)
            sub_end = sub.get("end_time", 0)
            sub_dur = sub_end - sub_start
            if sub_dur > 0.01:
                significant.append(sub)

            # Check nested
            for subsub in sub.get("subsegments", []):
                ss_start = subsub.get("start_time", 0)
                ss_end = subsub.get("end_time", 0)
                ss_dur = ss_end - ss_start
                if ss_dur > 0.01:
                    significant.append({"name": f"  └─{subsub['name']}", **subsub})

        print(f"\n  Significant subsegments (>{10}ms):")
        for sub in sorted(significant, key=lambda x: x.get("start_time", 0)):
            sub_start = sub.get("start_time", 0)
            sub_end = sub.get("end_time", 0)
            sub_dur = sub_end - sub_start
            name = sub.get("name", "?")
            ts = datetime.fromtimestamp(sub_start, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            te = datetime.fromtimestamp(sub_end, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"    {ts} → {te}  {sub_dur*1000:>8.1f}ms  {name}")

        # Timeline: first send, first receive, last send
        sends = []
        receives = []
        for sub in subsegments:
            name = sub.get("name", "")
            if "http send" in name:
                sends.append(sub.get("start_time", 0))
            if "http receive" in name:
                receives.append((sub.get("start_time", 0), sub.get("end_time", 0)))

        if sends:
            first_send = min(sends)
            last_send = max(sends)
            print(f"\n  Timeline:")
            print(f"    Segment start:  {datetime.fromtimestamp(seg_start, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]}")
            print(f"    First send:     {datetime.fromtimestamp(first_send, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]}  (+{(first_send-seg_start)*1000:.0f}ms)")
            if receives:
                for i, (rs, re) in enumerate(sorted(receives)):
                    rdur = re - rs
                    print(f"    Receive [{i}]:    {datetime.fromtimestamp(rs, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]} → {datetime.fromtimestamp(re, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]}  ({rdur*1000:.0f}ms)")
            print(f"    Last send:      {datetime.fromtimestamp(last_send, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]}  (+{(last_send-seg_start)*1000:.0f}ms)")
            print(f"    Segment end:    {datetime.fromtimestamp(seg_end, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]}  (+{(seg_end-seg_start)*1000:.0f}ms)")
            print(f"    Total sends: {len(sends)}  Total receives: {len(receives)}")
