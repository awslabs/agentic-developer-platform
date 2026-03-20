#!/usr/bin/env python3
"""Analyze the 39.2s X-Ray trace to understand the overhead breakdown.

Writes results to /tmp/trace-analysis.txt
"""

import json
from datetime import datetime, timezone

OUTPUT = "/tmp/trace-analysis.txt"


def ts(unix_ts):
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


with open("/tmp/trace-39s-detail.json") as f:
    data = json.load(f)

trace = data["Traces"][0]
seg = json.loads(trace["Segments"][0]["Document"])

lines = []


def out(s=""):
    lines.append(s)


# X-Ray trace-level timing
xray_duration = trace.get("Duration", 39.242)
seg_start = seg["start_time"]
seg_end = seg["end_time"]
seg_dur = seg_end - seg_start

out("=" * 70)
out("X-RAY TRACE ANALYSIS: 1-69a0df76-a769b88b10b64223fb0694df")
out("=" * 70)
out()
out(f"X-Ray Duration (client-perceived): {xray_duration:.3f}s")
out(f"X-Ray ResponseTime:                17.787s")
out(f"X-Ray Overhead:                    21.455s")
out()

# Segment analysis
out("--- SEGMENT (pod-level) ---")
out(f"Name:     {seg.get('name')}")
out(f"Origin:   {seg.get('origin')}")
out(f"Start:    {ts(seg_start)}")
out(f"End:      {ts(seg_end)}")
out(f"Duration: {seg_dur:.3f}s")
out(f"Instance: {seg.get('aws', {}).get('ec2', {}).get('instance_id', '?')}")
out(f"AZ:       {seg.get('aws', {}).get('ec2', {}).get('availability_zone', '?')}")
out()

# Subsegment categorization
subs = seg.get("subsegments", [])
sends = []
receives = []
for sub in subs:
    name = sub.get("name", "")
    if "http send" in name:
        sends.append(sub)
    elif "http receive" in name:
        receives.append(sub)

out(f"Subsegments: {len(subs)} total ({len(sends)} sends, {len(receives)} receives)")
out()

# Analyze receives (where the real work happens)
out("--- HTTP RECEIVES (where time is spent) ---")
for i, recv in enumerate(receives):
    r_start = recv.get("start_time", 0)
    r_end = recv.get("end_time", 0)
    r_dur = r_end - r_start if r_end and r_start else 0
    out(f"Receive #{i}: {ts(r_start)} -> {ts(r_end)} ({r_dur:.3f}s)")

    for nested in recv.get("subsegments", []):
        n_name = nested.get("name", "?")
        n_start = nested.get("start_time", 0)
        n_end = nested.get("end_time", 0)
        n_dur = n_end - n_start if n_end and n_start else 0
        out(f"  └─ {n_name}: {ts(n_start)} -> {ts(n_end)} ({n_dur:.3f}s)")

        for deep in nested.get("subsegments", []):
            d_name = deep.get("name", "?")
            d_start = deep.get("start_time", 0)
            d_end = deep.get("end_time", 0)
            d_dur = d_end - d_start if d_end and d_start else 0
            out(f"      └─ {d_name}: {ts(d_start)} -> {ts(d_end)} ({d_dur:.3f}s)")

out()

# Find the downstream call (nested inside a receive)
downstream_start = None
downstream_end = None
for recv in receives:
    for nested in recv.get("subsegments", []):
        ns = nested.get("start_time", 0)
        ne = nested.get("end_time", 0)
        nd = ne - ns if ne and ns else 0
        if nd > 1:
            downstream_start = ns
            downstream_end = ne
            downstream_name = nested.get("name", "?")

# Find the main streaming receive (the one with real duration)
main_recv_start = None
main_recv_end = None
for recv in receives:
    rs = recv.get("start_time", 0)
    re = recv.get("end_time", 0)
    rd = re - rs if re and rs else 0
    if rd > 1:
        main_recv_start = rs
        main_recv_end = re

out("=" * 70)
out("TIMELINE RECONSTRUCTION")
out("=" * 70)
out()

# The trace has TWO Bedrock calls in one trace:
# 1. invoke-with-response-stream (streaming Opus) — the main segment
# 2. invoke (non-streaming) — nested downstream call that starts AFTER segment ends

out("This trace contains TWO sequential Bedrock API calls:")
out()
out("REQUEST 1: invoke-with-response-stream (streaming Opus)")
out(f"  Segment start:  {ts(seg_start)}")
if main_recv_end:
    out(f"  Streaming recv: {ts(main_recv_start)} -> {ts(main_recv_end)} ({main_recv_end - main_recv_start:.3f}s)")
out(f"  Segment end:    {ts(seg_end)}")
out(f"  Duration:       {seg_dur:.3f}s")
out()

if downstream_start and downstream_end:
    out(f"REQUEST 2: invoke (non-streaming, nested downstream)")
    out(f"  Downstream to:  {downstream_name}")
    out(f"  Start:          {ts(downstream_start)}")
    out(f"  End:            {ts(downstream_end)}")
    out(f"  Duration:       {downstream_end - downstream_start:.3f}s")
    out(f"  Gap after R1:   {downstream_start - seg_end:.3f}s")
    out()

    total_app = downstream_end - seg_start
    out("=" * 70)
    out("OVERHEAD BREAKDOWN")
    out("=" * 70)
    out()
    out(f"X-Ray total Duration:              {xray_duration:.3f}s")
    out(f"App processing (seg start->ds end): {total_app:.3f}s")
    out(f"Network overhead (X-Ray - App):     {xray_duration - total_app:.3f}s")
    out()
    out("Breakdown:")
    out(f"  CloudFront ingress (before pod):  ~{xray_duration - total_app:.1f}s")
    out(f"  Request 1 (streaming Opus):        {seg_dur:.1f}s")
    out(f"  Gap between requests:              {downstream_start - seg_end:.1f}s")
    out(f"  Request 2 (non-streaming):         {downstream_end - downstream_start:.1f}s")
    out(f"  CloudFront egress (after pod):    ~0.0s (included in streaming)")
    out(f"                                    --------")
    out(f"  Total:                             {xray_duration:.1f}s")
    out()
    out("=" * 70)
    out("KEY FINDING")
    out("=" * 70)
    out()
    out("The 39.2s is NOT one slow request. It's TWO back-to-back Bedrock calls")
    out("on the same HTTP connection, traced as a single X-Ray trace:")
    out()
    out(f"  1st call (streaming):  {seg_dur:.1f}s  (Bedrock Opus response)")
    out(f"  2nd call (invoke):     {downstream_end - downstream_start:.1f}s  (Bedrock Opus non-streaming)")
    out(f"  Network overhead:      ~{xray_duration - total_app:.1f}s  (CloudFront VPC Origin)")
    out()
    out("The ~4s network overhead is the CloudFront VPC Origin connection setup.")
    out("The 21.5s 'overhead' X-Ray reports is actually the 2nd Bedrock call (17.3s)")
    out("plus the ~4s CloudFront latency. It's NOT idle time or queuing.")

with open(OUTPUT, "w") as f:
    f.write("\n".join(lines))

print(f"Analysis written to {OUTPUT}")
print()
# Also print to stdout
print("\n".join(lines))
