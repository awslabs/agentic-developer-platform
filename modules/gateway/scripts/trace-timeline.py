#!/usr/bin/env python3
"""Build a complete timeline of the 39.2s trace to find the 21.5s overhead."""

import json
from datetime import datetime, timezone


def ts(unix_ts):
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


with open("/tmp/trace-39s-detail.json") as f:
    data = json.load(f)

trace = data["Traces"][0]
seg = json.loads(trace["Segments"][0]["Document"])

# X-Ray trace-level timing
xray_duration = 39.242
xray_response_time = 17.787

# Segment timing (pod-level)
seg_start = seg["start_time"]  # 1772150646.078
seg_end = seg["end_time"]  # 1772150663.865

# The X-Ray Duration is measured from when the FIRST segment starts
# to when the LAST segment/subsegment ends.
# With only 1 segment, Duration should equal segment duration... unless
# there are subsegments that extend beyond the segment end.

# Find ALL timestamps in the trace
all_times = []
all_times.append(("seg_start", seg_start))
all_times.append(("seg_end", seg_end))

subs = seg.get("subsegments", [])
for i, sub in enumerate(subs):
    s = sub.get("start_time", 0)
    e = sub.get("end_time", 0)
    name = sub.get("name", "?")[:50]
    if s:
        all_times.append((f"sub[{i}]_start ({name})", s))
    if e:
        all_times.append((f"sub[{i}]_end ({name})", e))

    for j, nested in enumerate(sub.get("subsegments", [])):
        ns = nested.get("start_time", 0)
        ne = nested.get("end_time", 0)
        nname = nested.get("name", "?")[:40]
        if ns:
            all_times.append((f"  nested[{i}.{j}]_start ({nname})", ns))
        if ne:
            all_times.append((f"  nested[{i}.{j}]_end ({nname})", ne))

        for k, deep in enumerate(nested.get("subsegments", [])):
            ds = deep.get("start_time", 0)
            de = deep.get("end_time", 0)
            dname = deep.get("name", "?")[:30]
            if ds:
                all_times.append((f"    deep[{i}.{j}.{k}]_start ({dname})", ds))
            if de:
                all_times.append((f"    deep[{i}.{j}.{k}]_end ({dname})", de))

# Sort by timestamp
all_times.sort(key=lambda x: x[1])

earliest = all_times[0][1]
latest = all_times[-1][1]

print("=== FULL TRACE TIMELINE ===")
print(f"Earliest timestamp: {ts(earliest)} ({earliest})")
print(f"Latest timestamp:   {ts(latest)} ({latest})")
print(f"Span: {latest - earliest:.3f}s")
print(f"X-Ray reported Duration: {xray_duration}s")
print(f"X-Ray reported ResponseTime: {xray_response_time}s")
print()

# Show key events only (skip the hundreds of 0-duration sends)
print("=== KEY EVENTS (non-zero duration or boundaries) ===")
seen_times = set()
for label, t in all_times:
    # Skip duplicate timestamps (the 288 sends are all at similar times)
    t_rounded = round(t, 1)
    if "http send" in label and t_rounded in seen_times:
        continue
    seen_times.add(t_rounded)

    offset = t - earliest
    print(f"  +{offset:>8.3f}s  {ts(t)}  {label}")

print()
print("=== OVERHEAD BREAKDOWN ===")
print(f"Main segment: {ts(seg_start)} -> {ts(seg_end)} = {seg_end - seg_start:.3f}s")
print(f"  This is the streaming response from pod to client")
print()

# The downstream call to 10.0.10.81:8080 (17.278s) starts AFTER seg_end
# This is a SECOND request on the same trace
downstream_start = 1772150663.9955297
downstream_end = 1772150681.2738287
print(f"Downstream call: {ts(downstream_start)} -> {ts(downstream_end)} = "
      f"{downstream_end - downstream_start:.3f}s")
print(f"  Starts {downstream_start - seg_end:.3f}s after main segment ends")
print(f"  This is a SECOND Bedrock call (non-streaming invoke)")
print()

total_span = downstream_end - seg_start
print(f"Total from seg_start to downstream_end: {total_span:.3f}s")
print(f"  Main segment: {seg_end - seg_start:.3f}s")
print(f"  Gap between: {downstream_start - seg_end:.3f}s")
print(f"  Downstream: {downstream_end - downstream_start:.3f}s")
print(f"  Sum: {(seg_end - seg_start) + (downstream_start - seg_end) + (downstream_end - downstream_start):.3f}s")
print()

# X-Ray Duration = 39.242s but our span is only 35.196s
# The remaining ~4s must be CloudFront overhead
xray_implied_start = downstream_end - xray_duration
print(f"X-Ray implied request start: {ts(xray_implied_start)} "
      f"({seg_start - xray_implied_start:.3f}s before segment start)")
print(f"  This {seg_start - xray_implied_start:.3f}s is CloudFront -> VPC Origin -> ALB -> Pod latency")
print()
print("=== SUMMARY ===")
print(f"Total X-Ray Duration: {xray_duration:.3f}s")
print(f"  CloudFront ingress overhead: ~{seg_start - xray_implied_start:.1f}s")
print(f"  1st request (streaming Opus): {seg_end - seg_start:.1f}s")
print(f"  Gap between requests: {downstream_start - seg_end:.1f}s")
print(f"  2nd request (non-streaming): {downstream_end - downstream_start:.1f}s")
print(f"  CloudFront egress overhead: ~{xray_duration - total_span - (seg_start - xray_implied_start):.1f}s")
