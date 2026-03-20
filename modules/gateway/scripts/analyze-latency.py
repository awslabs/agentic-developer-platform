#!/usr/bin/env python3
"""Analyze latency from chat logs and gateway pod logs."""

import json
import glob
import subprocess
import sys
from collections import defaultdict

CHAT_LOGS_DIR = "/tmp/chatlogs"


def analyze_chat_logs():
    """Parse S3 chat logs for latency breakdown by model."""
    files = glob.glob(f"{CHAT_LOGS_DIR}/*.json")
    if not files:
        print("No chat logs found. Run: aws s3 sync s3://bedrockgw-dev-chat-logs/acme/.../2026/02/26/ /tmp/chatlogs/")
        return

    records = []
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
            records.append({
                "request_id": data.get("request_id", ""),
                "timestamp": data.get("timestamp", ""),
                "model": data.get("model", "unknown"),
                "latency_ms": data.get("latency_ms", 0),
                "input_tokens": data.get("response", {}).get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("response", {}).get("usage", {}).get("output_tokens", 0),
                "api_format": data.get("api_format", ""),
            })
        except Exception:
            pass

    records.sort(key=lambda r: r["timestamp"])

    # Group by model
    by_model = defaultdict(list)
    for r in records:
        by_model[r["model"]].append(r)

    print("=" * 90)
    print("CHAT LOG LATENCY ANALYSIS (S3 logs)")
    print("=" * 90)
    print(f"Total requests: {len(records)}")
    print()

    for model, reqs in sorted(by_model.items()):
        latencies = [r["latency_ms"] for r in reqs]
        latencies.sort()
        avg = sum(latencies) / len(latencies)
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        total_input = sum(r["input_tokens"] for r in reqs)
        total_output = sum(r["output_tokens"] for r in reqs)

        print(f"Model: {model}")
        print(f"  Requests: {len(reqs)}")
        print(f"  Latency (ms):  avg={avg:.0f}  p50={p50:.0f}  p95={p95:.0f}  min={latencies[0]:.0f}  max={latencies[-1]:.0f}")
        print(f"  Tokens:  total_input={total_input}  total_output={total_output}")
        print()

    # Show 10 slowest requests
    records.sort(key=lambda r: r["latency_ms"], reverse=True)
    print("-" * 90)
    print("TOP 10 SLOWEST REQUESTS")
    print("-" * 90)
    print(f"{'Latency(ms)':>12} {'Model':<45} {'In Tok':>8} {'Out Tok':>8} {'Time'}")
    for r in records[:10]:
        ts = r["timestamp"][-15:-1] if r["timestamp"] else ""
        model_short = r["model"][:44]
        print(f"{r['latency_ms']:>12.0f} {model_short:<45} {r['input_tokens']:>8} {r['output_tokens']:>8} {ts}")


def analyze_gateway_logs():
    """Pull gateway pod logs and extract timing breakdowns."""
    print()
    print("=" * 90)
    print("GATEWAY POD LOG ANALYSIS (kubectl logs)")
    print("=" * 90)

    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "bedrockgw", "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True, text=True, timeout=10,
        )
        pods = result.stdout.strip().split()
    except Exception as e:
        print(f"Failed to get pods: {e}")
        return

    all_entries = []
    for pod in pods:
        try:
            result = subprocess.run(
                ["kubectl", "logs", "-n", "bedrockgw", pod, "--since=4h"],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                    if d.get("event") == "request_end" and "/model/" in d.get("path", ""):
                        all_entries.append({
                            "path": d.get("path", "")[:80],
                            "status": d.get("status_code", 0),
                            "latency_ms": d.get("latency_ms", 0),
                            "timings": d.get("timings", {}),
                            "timestamp": d.get("timestamp", ""),
                            "request_id": d.get("request_id", ""),
                        })
                except Exception:
                    pass
        except Exception as e:
            print(f"Failed to get logs from {pod}: {e}")

    if not all_entries:
        print("No request_end logs found for /model/ paths in the last 4 hours.")
        print("(Budget-blocked requests don't reach LoggingMiddleware)")
        return

    all_entries.sort(key=lambda e: e["timestamp"])

    print(f"Total completed proxy requests: {len(all_entries)}")
    print()
    print(f"{'Latency(ms)':>12} {'Status':>6} {'Timings':<50} {'Path'}")
    print("-" * 120)
    for e in all_entries[-20:]:  # Last 20
        timings_str = ", ".join(f"{k}={v:.0f}ms" for k, v in e["timings"].items()) if e["timings"] else "N/A"
        print(f"{e['latency_ms']:>12.1f} {e['status']:>6} {timings_str:<50} {e['path']}")


if __name__ == "__main__":
    analyze_chat_logs()
    analyze_gateway_logs()
