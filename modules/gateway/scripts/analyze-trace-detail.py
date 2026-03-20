#!/usr/bin/env python3
"""Analyze a single X-Ray trace in detail to understand the 21.5s overhead.

Fetches the most recent high-overhead trace and breaks down timing
to show where time is spent outside the Bedrock response.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone


def fetch_recent_traces(hours=4):
    """Fetch recent trace summaries sorted by duration."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
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
    # Sort by duration descending
    summaries.sort(key=lambda x: x.get("Duration", 0), reverse=True)
    return summaries


def fetch_trace_detail(trace_id):
    """Fetch full trace segments for a given trace ID."""
    cmd = [
        "aws", "xray", "batch-get-traces",
        "--trace-ids", trace_id,
        "--region", "us-east-1",
        "--output", "json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout)


def analyze_trace(trace_data):
    """Deep analysis of trace segments and subsegments."""
    traces = trace_data.get("Traces", [])
    if not traces:
        print("No trace data found")
        return

    trace = traces[0]
    print(f"Trace ID: {trace['Id']}")
    print(f"X-Ray Duration: {trace.get('Duration', '?')}s")
    print(f"Segments: {len(trace.get('Segments', []))}")
    print("=" * 80)

    for seg in trace.get("Segments", []):
        doc = json.loads(seg["Document"])
        analyze_segment(doc, depth=0)


def ts_to_str(ts):
    """Convert unix timestamp to readable time."""
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%H:%M:%S.%f")[:-3]


def analyze_segment(doc, depth=0):
    """Recursively analyze a segment/subsegment."""
    indent = "  " * depth
    name = doc.get("name", "?")
    start = doc.get("start_time", 0)
    end = doc.get("end_time", 0)
    dur = end - start if end and start else 0
    origin = doc.get("origin", "")
    in_progress = doc.get("in_progress", False)

    # Header
    origin_str = f" ({origin})" if origin else ""
    progress_str = " [IN PROGRESS]" if in_progress else ""
    print(f"\n{indent}{'='*60}")
    print(f"{indent}SEGMENT: {name}{origin_str}{progress_str}")
    print(f"{indent}  Start: {ts_to_str(start)}  End: {ts_to_str(end)}")
    print(f"{indent}  Duration: {dur:.3f}s")

    # HTTP info
    http = doc.get("http", {})
    if http:
        req = http.get("request", {})
        resp = http.get("response", {})
        if req:
            print(f"{indent}  HTTP: {req.get('method', '?')} {req.get('url', '?')[:100]}")
        if resp:
            print(f"{indent}  Status: {resp.get('status', '?')}  Content-Length: {resp.get('content_length', '?')}")

    # AWS info
    aws = doc.get("aws", {})
    if aws:
        print(f"{indent}  AWS: {json.dumps(aws)[:200]}")

    # Annotations
    annotations = doc.get("annotations", {})
    if annotations:
        print(f"{indent}  Annotations: {json.dumps(annotations)[:200]}")

    # Metadata
    metadata = doc.get("metadata", {})
    if metadata:
        for ns, vals in metadata.items():
            print(f"{indent}  Metadata[{ns}]: {json.dumps(vals)[:200]}")

    # Subsegments - collect and sort by start_time
    subsegments = doc.get("subsegments", [])
    if not subsegments:
        return

    # Categorize subsegments
    sends = []
    receives = []
    named = []
    for sub in subsegments:
        sub_name = sub.get("name", "?")
        if "http send" in sub_name:
            sends.append(sub)
        elif "http receive" in sub_name:
            receives.append(sub)
        else:
            named.append(sub)

    print(f"\n{indent}  Subsegments: {len(subsegments)} total "
          f"({len(sends)} sends, {len(receives)} receives, {len(named)} named)")

    # Show sends summary (they're all 0.000s so just count them)
    if sends:
        send_starts = [s.get("start_time", 0) for s in sends if s.get("start_time")]
        send_ends = [s.get("end_time", 0) for s in sends if s.get("end_time")]
        if send_starts and send_ends:
            print(f"{indent}  HTTP sends: {len(sends)} chunks, "
                  f"first={ts_to_str(min(send_starts))}, last={ts_to_str(max(send_ends))}")

    # Show receives in detail (these are where time is spent)
    for recv in receives:
        r_start = recv.get("start_time", 0)
        r_end = recv.get("end_time", 0)
        r_dur = r_end - r_start if r_end and r_start else 0
        print(f"{indent}  HTTP receive: {ts_to_str(r_start)} -> {ts_to_str(r_end)} "
              f"({r_dur:.3f}s)")

        # Check for nested subsegments in receive
        for nested in recv.get("subsegments", []):
            n_name = nested.get("name", "?")
            n_start = nested.get("start_time", 0)
            n_end = nested.get("end_time", 0)
            n_dur = n_end - n_start if n_end and n_start else 0
            ns = nested.get("namespace", "")
            print(f"{indent}    └─ {n_name} [{ns}] {n_dur:.3f}s "
                  f"({ts_to_str(n_start)} -> {ts_to_str(n_end)})")

            # Go one more level
            for deep in nested.get("subsegments", []):
                d_name = deep.get("name", "?")
                d_start = deep.get("start_time", 0)
                d_end = deep.get("end_time", 0)
                d_dur = d_end - d_start if d_end and d_start else 0
                print(f"{indent}        └─ {d_name} {d_dur:.3f}s "
                      f"({ts_to_str(d_start)} -> {ts_to_str(d_end)})")

    # Show named subsegments in detail
    for sub in named:
        analyze_segment(sub, depth=depth + 1)

    # Timeline gap analysis
    print(f"\n{indent}  --- TIMELINE GAP ANALYSIS ---")
    all_subs = sorted(subsegments, key=lambda s: s.get("start_time", 0))

    # Find the actual Bedrock call (the receive with real duration)
    bedrock_start = None
    bedrock_end = None
    for recv in receives:
        r_dur = (recv.get("end_time", 0) or 0) - (recv.get("start_time", 0) or 0)
        if r_dur > 1.0:  # The real Bedrock response
            bedrock_start = recv.get("start_time", 0)
            bedrock_end = recv.get("end_time", 0)
            break

    if bedrock_start and start:
        pre_bedrock = bedrock_start - start
        print(f"{indent}  Segment start -> Bedrock receive start: {pre_bedrock:.3f}s "
              f"(this is the OVERHEAD before Bedrock responds)")

    if bedrock_end and end:
        post_bedrock = end - bedrock_end
        print(f"{indent}  Bedrock receive end -> Segment end: {post_bedrock:.3f}s "
              f"(post-processing / streaming to client)")

    if bedrock_start and bedrock_end:
        bedrock_dur = bedrock_end - bedrock_start
        print(f"{indent}  Bedrock response time: {bedrock_dur:.3f}s")

    if start and end and bedrock_start and bedrock_end:
        total = end - start
        bedrock_dur = bedrock_end - bedrock_start
        overhead = total - bedrock_dur
        print(f"{indent}  Total segment: {total:.3f}s, Bedrock: {bedrock_dur:.3f}s, "
              f"Overhead: {overhead:.3f}s")

    # Check first send vs segment start
    if sends:
        first_send_start = min(s.get("start_time", float("inf")) for s in sends)
        if first_send_start and start:
            gap = first_send_start - start
            print(f"{indent}  Segment start -> first HTTP send: {gap:.3f}s")


def main():
    # Get the trace ID from args or find the worst recent one
    if len(sys.argv) > 1:
        trace_id = sys.argv[1]
    else:
        print("Fetching recent traces to find the worst overhead...\n")
        summaries = fetch_recent_traces()
        if not summaries:
            print("No traces found")
            return

        # Show top 5
        print(f"{'Duration':>10} {'RespTime':>10} {'Overhead':>10} TraceId")
        print("-" * 80)
        for t in summaries[:10]:
            dur = t.get("Duration", 0)
            resp = t.get("ResponseTime", 0)
            overhead = dur - resp
            tid = t.get("Id", "?")
            print(f"{dur:>10.3f}s {resp:>10.3f}s {overhead:>10.3f}s {tid}")

        # Pick the one with highest overhead
        worst = max(summaries, key=lambda t: t.get("Duration", 0) - t.get("ResponseTime", 0))
        trace_id = worst["Id"]
        overhead = worst["Duration"] - worst["ResponseTime"]
        print(f"\nAnalyzing worst overhead trace: {trace_id} "
              f"(overhead={overhead:.3f}s)\n")

    detail = fetch_trace_detail(trace_id)
    analyze_trace(detail)


if __name__ == "__main__":
    main()
