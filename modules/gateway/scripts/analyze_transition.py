"""Analyze all Opus requests from today to find the latency transition pattern."""
import json
import subprocess
import sys

# Get all log files from today
result = subprocess.run(
    ["aws", "s3", "ls", "s3://bedrockgw-dev-chat-logs/acme/94c8f418-90d1-701c-e93d-a65df61d91d9/2026/02/27/",
     "--recursive"],
    capture_output=True, text=True,
)

lines = result.stdout.strip().split("\n")
files = []
for line in lines:
    parts = line.split()
    if len(parts) >= 4:
        files.append((parts[0], parts[1], int(parts[2]), parts[3]))

# Sort by timestamp
files.sort(key=lambda x: (x[0], x[1]))

# Sample every Nth file to get a timeline
# Download and check each one
prefix = "s3://bedrockgw-dev-chat-logs/"
results = []

for date, time_str, size, path in files:
    fname = path.split("/")[-1]
    local = f"/tmp/tl_{fname}"
    r = subprocess.run(
        ["aws", "s3", "cp", prefix + path, local],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        continue

    with open(local) as f:
        data = json.load(f)

    model = data.get("model", "?")
    if "opus" not in model:
        continue

    ts = data.get("timestamp", "?")
    latency = data.get("latency_ms", 0)
    req = data.get("request", {})
    msgs = req.get("messages", [])
    msg_count = len(msgs)
    total_chars = sum(len(json.dumps(m)) for m in msgs)
    tools = len(req.get("tools", []))
    sys_size = len(str(req.get("system", "")))

    resp = data.get("response", {})
    usage = resp.get("usage", {})
    in_tok = usage.get("input_tokens", "?")
    out_tok = usage.get("output_tokens", "?")

    lat_str = f"{latency:.0f}" if isinstance(latency, (int, float)) else str(latency)
    print(f"{ts[:19]}  lat={lat_str:>8}ms  msgs={msg_count:>3}  msg_chars={total_chars:>8}  sys={sys_size:>6}  tools={tools:>3}  in_tok={in_tok}  out_tok={out_tok}")

print("\nDone.")
