import json

for label, path in [("FAST (13:39)", "/tmp/fast_opus.json"), ("SLOW (14:08)", "/tmp/slow_opus.json")]:
    with open(path) as f:
        data = json.load(f)
    print(f"=== {label} ===")
    req = data.get("request", {})
    print(f"Request keys: {sorted(req.keys())}")
    print(f"max_tokens: {req.get('max_tokens')}")
    print(f"anthropic_version: {req.get('anthropic_version')}")
    msgs = req.get("messages", [])
    print(f"message count: {len(msgs)}")
    tools = req.get("tools", [])
    print(f"tools count: {len(tools)}")
    sys_prompt = req.get("system", "")
    if isinstance(sys_prompt, list):
        sys_size = sum(len(str(s)) for s in sys_prompt)
    else:
        sys_size = len(str(sys_prompt))
    print(f"system prompt chars: {sys_size}")
    total_msg_chars = sum(len(json.dumps(m)) for m in msgs)
    print(f"total message chars: {total_msg_chars}")

    resp = data.get("response", {})
    print(f"Response keys: {sorted(resp.keys())}")
    usage = resp.get("usage", {})
    print(f"Usage: {usage}")
    print()
