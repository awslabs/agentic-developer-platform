#!/usr/bin/env python3
"""Fetch X-Ray traces and produce a CSV with per-component latency breakdown.

Steps:
1. Get trace summaries (last 24h, model invoke requests)
2. Fetch full trace details for up to 10 traces
3. Parse segments/subsegments to extract component timings
4. Write CSV
"""
import csv
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REGION = "us-east-1"
HOURS_BACK = 24
MAX_TRACES = 10


def aws_cli(args: list[str]) -> dict:
    result = subprocess.run(
        ["aws"] + args + ["--region", REGION, "--output", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"AWS CLI error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def get_trace_summaries() -> list[dict]:
    """Get trace summaries for model invoke requests."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HOURS_BACK)

    all_summaries = []
    next_token = None

    while True:
        cmd = [
            "xray", "get-trace-summaries",
            "--start-time", start.strftime("%Y-%m-%dT%H:%M:%S"),
            "--end-time", now.strftime("%Y-%m-%dT%H:%M:%S"),
            "--filter-expression", 'http.url CONTAINS "/model/" AND http.url CONTAINS "invoke"',
        ]
        if next_token:
            cmd += ["--next-token", next_token]

        data = aws_cli(cmd)
        summaries = data.get("TraceSummaries", [])
        all_summaries.extend(summaries)

        next_token = data.get("NextToken")
        if not next_token or len(all_summaries) >= 50:
            break

    return all_summaries


def get_trace_details(trace_ids: list[str]) -> list[dict]:
    """Fetch full trace details for given trace IDs (batch of 5)."""
    all_traces = []
    # X-Ray batch-get-traces accepts up to 5 IDs at a time
    for i in range(0, len(trace_ids), 5):
        batch = trace_ids[i : i + 5]
        data = aws_cli(["xray", "batch-get-traces", "--trace-ids"] + batch)
        all_traces.extend(data.get("Traces", []))
    return all_traces


def parse_trace(trace: dict) -> dict:
    """Parse a trace into component timings."""
    segments = trace.get("Segments", [])
    result = {
        "trace_id": trace.get("Id", "?"),
        "url": "",
        "model": "",
        "type": "",  # invoke or stream
        "http_status": "",
        "total_duration_s": 0,
        "response_time_s": 0,
        "cloudfront_ms": 0,
        "gateway_middleware_ms": 0,
        "auth_ms": 0,
        "model_resolve_ms": 0,
        "bedrock_ms": 0,
        "serialize_ms": 0,
        "other_gateway_ms": 0,
        "timestamp": "",
    }

    earliest_start = None
    latest_end = None

    for seg_wrapper in segments:
        doc = json.loads(seg_wrapper.get("Document", "{}"))
        seg_name = doc.get("name", "")
        seg_start = doc.get("start_time", 0)
        seg_end = doc.get("end_time", 0)
        seg_dur_ms = (seg_end - seg_start) * 1000

        if earliest_start is None or seg_start < earliest_start:
            earliest_start = seg_start
        if latest_end is None or seg_end > latest_end:
            latest_end = seg_end

        # Extract HTTP info from the main segment
        http = doc.get("http", {})
        if http.get("request", {}).get("url"):
            url = http["request"]["url"]
            result["url"] = url
            result["http_status"] = str(http.get("response", {}).get("status", ""))

            if "/model/" in url:
                result["model"] = url.split("/model/")[-1].split("/invoke")[0]
            if "invoke-with-response-stream" in url:
                result["type"] = "streaming"
            elif "/invoke" in url:
                result["type"] = "non-streaming"

        # Parse subsegments for component timings
        subsegments = doc.get("subsegments", [])
        for sub in subsegments:
            sub_name = sub.get("name", "")
            sub_start = sub.get("start_time", 0)
            sub_end = sub.get("end_time", 0)
            sub_dur_ms = (sub_end - sub_start) * 1000

            if sub_name == "auth":
                result["auth_ms"] = round(sub_dur_ms, 1)
            elif sub_name == "model_resolve":
                result["model_resolve_ms"] = round(sub_dur_ms, 1)
            elif sub_name in ("bedrock", "bedrock_ttfb"):
                result["bedrock_ms"] = round(sub_dur_ms, 1)
            elif sub_name == "serialize":
                result["serialize_ms"] = round(sub_dur_ms, 1)

            # Recurse one level deeper
            for subsub in sub.get("subsegments", []):
                subsub_name = subsub.get("name", "")
                subsub_start = subsub.get("start_time", 0)
                subsub_end = subsub.get("end_time", 0)
                subsub_dur_ms = (subsub_end - subsub_start) * 1000

                if subsub_name == "auth":
                    result["auth_ms"] = round(subsub_dur_ms, 1)
                elif subsub_name == "model_resolve":
                    result["model_resolve_ms"] = round(subsub_dur_ms, 1)
                elif subsub_name in ("bedrock", "bedrock_ttfb"):
                    result["bedrock_ms"] = round(subsub_dur_ms, 1)
                elif subsub_name == "serialize":
                    result["serialize_ms"] = round(subsub_dur_ms, 1)

        # Check if this is a CloudFront segment
        if seg_name == "CloudFront" or "cloudfront" in seg_name.lower():
            result["cloudfront_ms"] = round(seg_dur_ms, 1)

    if earliest_start and latest_end:
        result["total_duration_s"] = round(latest_end - earliest_start, 3)
        result["timestamp"] = datetime.fromtimestamp(earliest_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Calculate gateway overhead (total minus bedrock minus cloudfront)
    total_ms = result["total_duration_s"] * 1000
    known_ms = result["bedrock_ms"] + result["auth_ms"] + result["model_resolve_ms"] + result["serialize_ms"] + result["cloudfront_ms"]
    result["other_gateway_ms"] = round(max(0, total_ms - result["bedrock_ms"] - result["cloudfront_ms"]), 1)
    result["gateway_middleware_ms"] = round(max(0, result["other_gateway_ms"] - result["auth_ms"] - result["model_resolve_ms"] - result["serialize_ms"]), 1)

    return result


def main():
    print("Fetching trace summaries...", file=sys.stderr)
    summaries = get_trace_summaries()
    print(f"Found {len(summaries)} traces", file=sys.stderr)

    if not summaries:
        print("No traces found. Try increasing HOURS_BACK.", file=sys.stderr)
        sys.exit(0)

    # Sort by duration descending, pick top MAX_TRACES for variety
    summaries.sort(key=lambda x: x.get("Duration", 0), reverse=True)

    # Pick a mix: top 3 longest, bottom 3 shortest, 4 from middle
    selected = []
    if len(summaries) <= MAX_TRACES:
        selected = summaries
    else:
        selected.extend(summaries[:3])  # longest
        selected.extend(summaries[-3:])  # shortest
        mid = len(summaries) // 2
        selected.extend(summaries[mid - 2 : mid + 2])  # middle

    # Deduplicate
    seen = set()
    unique = []
    for s in selected:
        tid = s.get("Id")
        if tid not in seen:
            seen.add(tid)
            unique.append(s)
    selected = unique[:MAX_TRACES]

    trace_ids = [s["Id"] for s in selected]
    print(f"Fetching details for {len(trace_ids)} traces...", file=sys.stderr)
    traces = get_trace_details(trace_ids)
    print(f"Got {len(traces)} trace details", file=sys.stderr)

    # Parse all traces
    rows = []
    for trace in traces:
        row = parse_trace(trace)
        rows.append(row)

    # Sort by total duration descending
    rows.sort(key=lambda x: x["total_duration_s"], reverse=True)

    # Print summary to stderr
    print("\n=== Latency Breakdown Summary ===", file=sys.stderr)
    for r in rows:
        model_short = r["model"].split(".")[-1][:25] if "." in r["model"] else r["model"][:25]
        print(
            f"  {r['total_duration_s']:>7.1f}s total | bedrock={r['bedrock_ms']:>8.0f}ms | auth={r['auth_ms']:>5.0f}ms | "
            f"middleware={r['gateway_middleware_ms']:>5.0f}ms | {r['type']:<13} | {model_short}",
            file=sys.stderr,
        )

    # Write CSV
    csv_path = "scripts/latency-breakdown.csv"
    fieldnames = [
        "trace_id", "timestamp", "model", "type", "http_status",
        "total_duration_s", "bedrock_ms", "auth_ms", "model_resolve_ms",
        "serialize_ms", "cloudfront_ms", "gateway_middleware_ms", "other_gateway_ms", "url",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in fieldnames})

    print(f"\nCSV written to {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
