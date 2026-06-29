#!/usr/bin/env python3
"""Build latency breakdown CSV from CloudWatch application logs + X-Ray traces.

Combines:
- Application logs: auth, bedrock, serialize, total timings from X-Gateway-Timing
- X-Ray traces: Duration, ResponseTime, overhead analysis
"""
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta

REGION = "us-east-1"
LOG_GROUP = "/aws/containerinsights/bedrockgw-dev-eks-cluster/application"
HOURS_BACK = 24


def aws_cli(args: list[str]) -> str:
    result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
        ["aws"] + args + ["--region", REGION, "--output", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"AWS CLI error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def get_log_events() -> list[dict]:
    """Fetch request_end log events with timings."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HOURS_BACK)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    all_events = []
    next_token = None

    while True:
        cmd = [
            "logs", "filter-log-events",
            "--log-group-name", LOG_GROUP,
            "--start-time", str(start_ms),
            "--end-time", str(end_ms),
            "--filter-pattern", '"request_end" "timings" "/model/"',
            "--limit", "100",
        ]
        if next_token:
            cmd += ["--next-token", next_token]

        data = json.loads(aws_cli(cmd))
        all_events.extend(data.get("events", []))
        next_token = data.get("nextToken")
        if not next_token or len(all_events) >= 200:
            break

    return all_events


def parse_log_event(event: dict) -> dict | None:
    """Parse a CloudWatch log event into structured data."""
    msg = event.get("message", "")
    try:
        outer = json.loads(msg)
        inner_str = outer.get("log", "")
        inner = json.loads(inner_str)
    except (json.JSONDecodeError, TypeError):
        return None

    path = inner.get("path", "")
    if "/model/" not in path:
        return None

    timings = inner.get("timings", {})
    latency_ms = inner.get("latency_ms", 0)
    status = inner.get("status_code", 0)
    timestamp = inner.get("timestamp", "")

    # Extract model from path
    model = ""
    if "/model/" in path:
        model = path.split("/model/")[-1].split("/invoke")[0]

    req_type = "streaming" if "invoke-with-response-stream" in path else "non-streaming"

    return {
        "timestamp": timestamp,
        "model": model,
        "type": req_type,
        "http_status": status,
        "total_latency_ms": round(latency_ms, 1),
        "auth_ms": round(timings.get("auth", 0), 1),
        "model_resolve_ms": round(timings.get("model_resolve", 0), 1),
        "budget_check_ms": round(timings.get("budget_check", 0), 1),
        "ratelimit_check_ms": round(timings.get("ratelimit_check", 0), 1),
        "bedrock_ms": round(timings.get("bedrock", 0), 1),
        "bedrock_ttfb_ms": round(timings.get("bedrock_ttfb", 0), 1),
        "serialize_ms": round(timings.get("serialize", 0), 1),
        "logging_total_ms": round(timings.get("total", 0), 1),
        "path": path,
    }


def get_xray_summaries() -> list[dict]:
    """Get X-Ray trace summaries for correlation."""
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
        if not next_token:
            break
    return all_summaries


def main():
    # Get application logs
    print("Fetching application logs...", file=sys.stderr)
    events = get_log_events()
    print(f"Found {len(events)} log events", file=sys.stderr)

    rows = []
    for e in events:
        parsed = parse_log_event(e)
        if parsed:
            rows.append(parsed)

    print(f"Parsed {len(rows)} model requests", file=sys.stderr)

    if not rows:
        print("No model requests found in logs.", file=sys.stderr)
        sys.exit(0)

    # Get X-Ray summaries for overall stats
    print("Fetching X-Ray trace summaries...", file=sys.stderr)
    xray_summaries = get_xray_summaries()
    print(f"Found {len(xray_summaries)} X-Ray traces", file=sys.stderr)

    # Sort by latency descending
    rows.sort(key=lambda x: x["total_latency_ms"], reverse=True)

    # Select 10 representative: 3 longest, 3 shortest, 4 middle
    if len(rows) <= 10:
        selected = rows
    else:
        selected = []
        selected.extend(rows[:3])
        selected.extend(rows[-3:])
        mid = len(rows) // 2
        selected.extend(rows[mid - 2:mid + 2])
        # Deduplicate by timestamp
        seen = set()
        unique = []
        for r in selected:
            key = r["timestamp"]
            if key not in seen:
                seen.add(key)
                unique.append(r)
        selected = unique[:10]

    # Re-sort selected
    selected.sort(key=lambda x: x["total_latency_ms"], reverse=True)

    # Compute gateway overhead and estimated bedrock time for each
    for r in selected:
        if r["type"] == "non-streaming":
            # Non-streaming: bedrock_ms is accurate (time for invoke_model call)
            r["bedrock_estimated_ms"] = r["bedrock_ms"]
            r["gateway_overhead_ms"] = round(max(0, r["total_latency_ms"] - r["bedrock_ms"]), 1)
        else:
            # Streaming: bedrock_ttfb_ms is just stream object creation (<1ms)
            # Real bedrock time ≈ total_latency_ms - gateway overhead
            # logging_total_ms captures gateway processing time (before stream starts)
            r["bedrock_estimated_ms"] = round(r["total_latency_ms"] - r["logging_total_ms"], 1)
            r["gateway_overhead_ms"] = round(r["logging_total_ms"], 1)
        r["bedrock_pct"] = round(r["bedrock_estimated_ms"] / r["total_latency_ms"] * 100, 1) if r["total_latency_ms"] > 0 else 0

    # Print table
    print(f"\n{'='*160}", file=sys.stderr)
    print(f"{'#':>2} {'Model':<28} {'Type':<14} {'Total':>9} {'Bedrock':>9} {'GW OH':>8} {'Bdrk%':>6} {'Status':>6}", file=sys.stderr)
    print("-" * 120, file=sys.stderr)
    for i, r in enumerate(selected, 1):
        model_short = r["model"].split(".")[-1][:26] if "." in r["model"] else r["model"][:26]
        print(
            f"{i:>2} {model_short:<28} {r['type']:<14} "
            f"{r['total_latency_ms']:>8.0f}ms {r['bedrock_estimated_ms']:>8.0f}ms "
            f"{r['gateway_overhead_ms']:>7.0f}ms {r['bedrock_pct']:>5.1f}% "
            f"{r['http_status']:>6}",
            file=sys.stderr,
        )

    # Print X-Ray overall stats
    if xray_summaries:
        durs = sorted([s["Duration"] for s in xray_summaries])
        resps = sorted([s["ResponseTime"] for s in xray_summaries])
        print(f"\nX-Ray overall ({len(xray_summaries)} traces):", file=sys.stderr)
        print(f"  Duration:     avg={sum(durs)/len(durs):.1f}s  p50={durs[len(durs)//2]:.1f}s  p95={durs[int(len(durs)*0.95)]:.1f}s  max={durs[-1]:.1f}s", file=sys.stderr)
        print(f"  ResponseTime: avg={sum(resps)/len(resps):.1f}s  p50={resps[len(resps)//2]:.1f}s  p95={resps[int(len(resps)*0.95)]:.1f}s  max={resps[-1]:.1f}s", file=sys.stderr)

    # Print all-request stats from logs
    all_latencies = sorted([r["total_latency_ms"] for r in rows])
    streaming_latencies = sorted([r["total_latency_ms"] for r in rows if r["type"] == "streaming"])
    nonstream_latencies = sorted([r["total_latency_ms"] for r in rows if r["type"] == "non-streaming"])

    print(f"\nApplication log stats ({len(rows)} requests):", file=sys.stderr)
    print(f"  All:           avg={sum(all_latencies)/len(all_latencies):.0f}ms  p50={all_latencies[len(all_latencies)//2]:.0f}ms  p95={all_latencies[int(len(all_latencies)*0.95)]:.0f}ms  max={all_latencies[-1]:.0f}ms", file=sys.stderr)
    if streaming_latencies:
        print(f"  Streaming:     avg={sum(streaming_latencies)/len(streaming_latencies):.0f}ms  p50={streaming_latencies[len(streaming_latencies)//2]:.0f}ms  max={streaming_latencies[-1]:.0f}ms  ({len(streaming_latencies)} reqs)", file=sys.stderr)
    if nonstream_latencies:
        print(f"  Non-streaming: avg={sum(nonstream_latencies)/len(nonstream_latencies):.0f}ms  p50={nonstream_latencies[len(nonstream_latencies)//2]:.0f}ms  max={nonstream_latencies[-1]:.0f}ms  ({len(nonstream_latencies)} reqs)", file=sys.stderr)

    # Bedrock time stats (non-streaming only, where we have accurate bedrock timing)
    bedrock_times = sorted([r["bedrock_ms"] for r in rows if r["type"] == "non-streaming" and r["bedrock_ms"] > 0])
    if bedrock_times:
        print(f"  Bedrock (non-stream): avg={sum(bedrock_times)/len(bedrock_times):.0f}ms  p50={bedrock_times[len(bedrock_times)//2]:.0f}ms  max={bedrock_times[-1]:.0f}ms", file=sys.stderr)

    # Write CSV
    csv_path = "scripts/latency-breakdown.csv"
    fieldnames = [
        "timestamp", "model", "type", "http_status",
        "total_latency_ms", "bedrock_estimated_ms", "gateway_overhead_ms",
        "bedrock_pct", "auth_ms", "budget_check_ms", "ratelimit_check_ms",
        "serialize_ms", "bedrock_ms", "bedrock_ttfb_ms",
        "logging_total_ms", "path",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in selected:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    print(f"\nCSV written to {csv_path} ({len(selected)} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
