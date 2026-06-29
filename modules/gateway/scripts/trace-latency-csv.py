#!/usr/bin/env python3
"""Fetch X-Ray traces and produce a CSV with per-component latency breakdown.

Analyzes 10 representative traces from the last 24h showing:
- Total request duration (client perspective)
- Time to first byte (TTFB) = X-Ray ResponseTime
- Stream delivery time = Duration - ResponseTime
- Backend processing time (from subsegment analysis)
- Gateway overhead = Total - Backend
"""
import csv
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REGION = "us-east-1"
HOURS_BACK = 24
MAX_TRACES = 10


def aws_cli(args: list[str]) -> str:
    result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
        ["aws"] + args + ["--region", REGION, "--output", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"AWS CLI error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def get_all_summaries() -> list[dict]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HOURS_BACK)
    all_summaries = []
    next_token = None
    while True:
        cmd = [
            "xray", "get-trace-summaries",
            "--start-time", start.strftime("%Y-%m-%dT%H:%M:%S"),
            "--end-time", now.strftime("%Y-%m-%dT%H:%M:%S"),
            "--filter-expression",
            'http.url CONTAINS "/model/" AND http.url CONTAINS "invoke"',
        ]
        if next_token:
            cmd += ["--next-token", next_token]
        data = json.loads(aws_cli(cmd))
        all_summaries.extend(data.get("TraceSummaries", []))
        next_token = data.get("NextToken")
        if not next_token or len(all_summaries) >= 200:
            break
    return all_summaries


def get_trace_details(trace_ids: list[str]) -> list[dict]:
    all_traces = []
    for i in range(0, len(trace_ids), 5):
        batch = trace_ids[i : i + 5]
        data = json.loads(aws_cli(["xray", "batch-get-traces", "--trace-ids"] + batch))
        all_traces.extend(data.get("Traces", []))
    return all_traces


def parse_trace_detail(trace: dict) -> dict:
    """Extract detailed timing from trace segments."""
    info = {
        "segment_duration_ms": 0,
        "alb_to_pod_ms": 0,
        "http_receive_wait_ms": 0,
        "stream_send_count": 0,
        "segment_start": 0,
        "segment_end": 0,
        "first_send_time": 0,
        "last_send_time": 0,
    }

    for seg_wrapper in trace.get("Segments", []):
        doc = json.loads(seg_wrapper.get("Document", "{}"))
        seg_start = doc.get("start_time", 0)
        seg_end = doc.get("end_time", 0)
        info["segment_duration_ms"] = round((seg_end - seg_start) * 1000, 1)
        info["segment_start"] = seg_start
        info["segment_end"] = seg_end

        subsegments = doc.get("subsegments", [])
        send_times = []

        for sub in subsegments:
            name = sub.get("name", "")
            sub_start = sub.get("start_time", 0)
            sub_end = sub.get("end_time", 0)
            dur_ms = (sub_end - sub_start) * 1000

            if "http send" in name:
                info["stream_send_count"] += 1
                send_times.append(sub_start)

            # Long http receive = waiting for backend response
            if "http receive" in name and dur_ms > 50:
                info["http_receive_wait_ms"] = max(
                    info["http_receive_wait_ms"], round(dur_ms, 1)
                )

            # Check nested subsegments for ALB→pod
            for subsub in sub.get("subsegments", []):
                subsub_name = subsub.get("name", "")
                subsub_start = subsub.get("start_time", 0)
                subsub_end = subsub.get("end_time", 0)
                subsub_dur_ms = (subsub_end - subsub_start) * 1000
                if ":" in subsub_name and subsub_dur_ms > 10:
                    info["alb_to_pod_ms"] = max(
                        info["alb_to_pod_ms"], round(subsub_dur_ms, 1)
                    )

        if send_times:
            info["first_send_time"] = min(send_times)
            info["last_send_time"] = max(send_times)

    return info


def main():
    print("Fetching trace summaries...", file=sys.stderr)
    summaries = get_all_summaries()
    print(f"Found {len(summaries)} model invoke traces", file=sys.stderr)

    if not summaries:
        print("No traces found.", file=sys.stderr)
        sys.exit(0)

    # Print overall stats
    all_durs = [s.get("Duration", 0) for s in summaries]
    all_resp = [s.get("ResponseTime", 0) for s in summaries]
    all_durs.sort()
    all_resp.sort()

    streaming = [s for s in summaries if "invoke-with-response-stream" in s.get("Http", {}).get("HttpURL", "")]
    non_streaming = [s for s in summaries if "invoke-with-response-stream" not in s.get("Http", {}).get("HttpURL", "") and "/invoke" in s.get("Http", {}).get("HttpURL", "")]

    print(f"\nOverall stats ({len(summaries)} traces):", file=sys.stderr)
    print(f"  Duration:     avg={sum(all_durs)/len(all_durs):.1f}s  p50={all_durs[len(all_durs)//2]:.1f}s  p95={all_durs[int(len(all_durs)*0.95)]:.1f}s  max={all_durs[-1]:.1f}s", file=sys.stderr)
    print(f"  ResponseTime: avg={sum(all_resp)/len(all_resp):.1f}s  p50={all_resp[len(all_resp)//2]:.1f}s  p95={all_resp[int(len(all_resp)*0.95)]:.1f}s  max={all_resp[-1]:.1f}s", file=sys.stderr)
    print(f"  Streaming: {len(streaming)}  Non-streaming: {len(non_streaming)}", file=sys.stderr)

    if streaming:
        s_durs = sorted([s["Duration"] for s in streaming])
        print(f"  Streaming duration:     avg={sum(s_durs)/len(s_durs):.1f}s  p50={s_durs[len(s_durs)//2]:.1f}s  max={s_durs[-1]:.1f}s", file=sys.stderr)
    if non_streaming:
        ns_durs = sorted([s["Duration"] for s in non_streaming])
        print(f"  Non-streaming duration: avg={sum(ns_durs)/len(ns_durs):.1f}s  p50={ns_durs[len(ns_durs)//2]:.1f}s  max={ns_durs[-1]:.1f}s", file=sys.stderr)

    # Select 10 diverse traces
    summaries.sort(key=lambda x: x.get("Duration", 0), reverse=True)
    if len(summaries) <= MAX_TRACES:
        selected = summaries
    else:
        selected = []
        selected.extend(summaries[:3])  # 3 longest
        selected.extend(summaries[-3:])  # 3 shortest
        mid = len(summaries) // 2
        selected.extend(summaries[mid - 2 : mid + 2])  # 4 middle
        seen = set()
        unique = []
        for s in selected:
            if s["Id"] not in seen:
                seen.add(s["Id"])
                unique.append(s)
        selected = unique[:MAX_TRACES]

    # Fetch full details
    trace_ids = [s["Id"] for s in selected]
    print(f"\nFetching details for {len(trace_ids)} traces...", file=sys.stderr)
    traces = get_trace_details(trace_ids)

    detail_map = {}
    for t in traces:
        detail_map[t["Id"]] = parse_trace_detail(t)

    # Build rows
    rows = []
    for s in selected:
        tid = s["Id"]
        url = s.get("Http", {}).get("HttpURL", "")
        model = ""
        if "/model/" in url:
            model = url.split("/model/")[-1].split("/invoke")[0]

        req_type = "streaming" if "invoke-with-response-stream" in url else "non-streaming"
        total_s = s.get("Duration", 0)
        resp_s = s.get("ResponseTime", 0)

        detail = detail_map.get(tid, {})

        # Backend time = best estimate from subsegments
        backend_ms = max(detail.get("alb_to_pod_ms", 0), detail.get("http_receive_wait_ms", 0))

        # For non-streaming with no subsegment data, use ResponseTime as backend estimate
        if backend_ms == 0 and req_type == "non-streaming":
            backend_ms = round(resp_s * 1000, 1)

        total_ms = total_s * 1000
        ttfb_ms = resp_s * 1000

        # Gateway overhead for TTFB path
        gw_overhead_ms = round(max(0, ttfb_ms - backend_ms), 1)

        # Stream delivery = time after first byte
        stream_delivery_ms = round(max(0, total_ms - ttfb_ms), 1)

        # Timestamp from detail
        ts = ""
        seg_start = detail.get("segment_start", 0)
        if seg_start:
            ts = datetime.fromtimestamp(seg_start, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )

        rows.append({
            "trace_id": tid,
            "timestamp": ts,
            "model": model,
            "type": req_type,
            "http_status": str(s.get("Http", {}).get("HttpStatus", "")),
            "total_duration_ms": round(total_ms, 0),
            "ttfb_ms": round(ttfb_ms, 0),
            "stream_delivery_ms": round(stream_delivery_ms, 0),
            "bedrock_backend_ms": round(backend_ms, 0),
            "gateway_overhead_ms": round(gw_overhead_ms, 0),
            "alb_to_pod_ms": detail.get("alb_to_pod_ms", 0),
            "http_receive_wait_ms": detail.get("http_receive_wait_ms", 0),
            "stream_chunks": detail.get("stream_send_count", 0),
        })

    rows.sort(key=lambda x: x["total_duration_ms"], reverse=True)

    # Print table
    print(f"\n{'='*140}", file=sys.stderr)
    hdr = f"{'#':>2} {'Model':<28} {'Type':<14} {'Total':>9} {'TTFB':>9} {'Stream':>9} {'Backend':>9} {'GW OH':>9} {'Chunks':>7} {'Status':>6}"
    print(hdr, file=sys.stderr)
    print("-" * 140, file=sys.stderr)
    for i, r in enumerate(rows, 1):
        model_short = r["model"].split(".")[-1][:26] if "." in r["model"] else r["model"][:26]
        print(
            f"{i:>2} {model_short:<28} {r['type']:<14} "
            f"{r['total_duration_ms']:>8.0f}ms {r['ttfb_ms']:>8.0f}ms "
            f"{r['stream_delivery_ms']:>8.0f}ms {r['bedrock_backend_ms']:>8.0f}ms "
            f"{r['gateway_overhead_ms']:>8.0f}ms {r['stream_chunks']:>6} "
            f"{r['http_status']:>6}",
            file=sys.stderr,
        )

    # Write CSV
    csv_path = "scripts/latency-breakdown.csv"
    fieldnames = [
        "trace_id", "timestamp", "model", "type", "http_status",
        "total_duration_ms", "ttfb_ms", "stream_delivery_ms",
        "bedrock_backend_ms", "gateway_overhead_ms",
        "alb_to_pod_ms", "http_receive_wait_ms", "stream_chunks",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"\nCSV written to {csv_path} ({len(rows)} rows)", file=sys.stderr)
    print("\nColumn descriptions:", file=sys.stderr)
    print("  total_duration_ms    = End-to-end request time (X-Ray Duration)", file=sys.stderr)
    print("  ttfb_ms              = Time to first byte (X-Ray ResponseTime)", file=sys.stderr)
    print("  stream_delivery_ms   = Time spent streaming after first byte (Duration - ResponseTime)", file=sys.stderr)
    print("  bedrock_backend_ms   = Estimated Bedrock processing time (from subsegment analysis)", file=sys.stderr)
    print("  gateway_overhead_ms  = Gateway overhead on TTFB path (TTFB - Backend)", file=sys.stderr)
    print("  alb_to_pod_ms        = ALB to pod connection time (from nested subsegment)", file=sys.stderr)
    print("  http_receive_wait_ms = HTTP receive wait time (largest receive subsegment)", file=sys.stderr)
    print("  stream_chunks        = Number of SSE chunks sent to client", file=sys.stderr)


if __name__ == "__main__":
    main()
