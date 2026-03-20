import json, subprocess

files = [
    "4daa79fa-036a-453c-a382-6bc89912b163.json",
    "f6fb62c3-1725-4209-abc8-60411a3483a5.json",
    "60cbc807-d9ca-40ab-b817-d4d38515967d.json",
    "d798ad2a-9ad4-4395-9ba9-a31219d31d5a.json",
    "1eeca398-cea1-46d7-83bf-6995a1efbcc1.json",
    "ff2385cf-7b15-4208-8a22-ef3741c57e6f.json",
    "f84bf39b-d02f-4c98-a740-434f451bf7df.json",
    "f9263a40-baa2-4caa-8c75-becd7c5feb07.json",
    "f40d6fc2-b1c5-439e-9f50-8786c9b87475.json",
    "f7011e65-9107-46bf-a55f-beffeeb10abb.json",
]

prefix = "s3://bedrockgw-dev-chat-logs/acme/94c8f418-90d1-701c-e93d-a65df61d91d9/2026/02/27/"

for fname in files:
    result = subprocess.run(
        ["aws", "s3", "cp", prefix + fname, "/tmp/chat_" + fname],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"FAILED: {fname}")
        continue

    with open("/tmp/chat_" + fname) as f:
        data = json.load(f)

    ts = data.get("timestamp", "?")
    model = data.get("model", "?")
    latency = data.get("latency_ms", "?")
    streaming = data.get("streaming", "N/A")

    resp = data.get("response_body", {})
    stop_reason = resp.get("stop_reason", "N/A") if isinstance(resp, dict) else "N/A"

    lat_str = f"{latency:.0f}" if isinstance(latency, (int, float)) else str(latency)
    print(f"{ts[:19]}  model={model:<50}  latency={lat_str:>8}ms  stop={stop_reason}")
