import json, subprocess

files = [
    "5e805f11-428f-4ac8-b6f7-2a387d9ab909.json",  # 13:19
    "41686a7d-78ea-492f-87b4-5295424aac86.json",  # 13:19
    "5ee02d2f-b0ee-4c5a-9739-ab86ffc261da.json",  # 13:39
    "0ceda324-52f8-4dbc-a39c-6d41df2ec772.json",  # 13:39
    "4d3a8020-78a4-4343-9ec5-917e8590c24c.json",  # 13:39
    "6a677d81-5551-47c0-b317-7538e2cb11f9.json",  # 14:09
    "e3cd00db-2ac7-469f-81b6-6055237b92e5.json",  # 14:09
    "2ecf4d03-b81f-4ea7-b755-ea9c69d537b0.json",  # 14:09
    "62dfe825-0bc2-4dee-ab58-61ae9254b4ac.json",  # 14:09
]

prefix = "s3://bedrockgw-dev-chat-logs/acme/94c8f418-90d1-701c-e93d-a65df61d91d9/2026/02/27/"

for fname in files:
    result = subprocess.run(
        ["aws", "s3", "cp", prefix + fname, "/tmp/tr_" + fname],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"FAILED: {fname}")
        continue

    with open("/tmp/tr_" + fname) as f:
        data = json.load(f)

    ts = data.get("timestamp", "?")
    model = data.get("model", "?")
    latency = data.get("latency_ms", "?")
    lat_str = f"{latency:.0f}" if isinstance(latency, (int, float)) else str(latency)
    print(f"{ts[:19]}  model={model:<50}  latency={lat_str:>8}ms")
