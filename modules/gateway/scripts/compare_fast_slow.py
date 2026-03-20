import json

for label, path in [("FAST (13:39)", "/tmp/fast_opus.json"), ("SLOW (14:08)", "/tmp/slow_opus.json")]:
    with open(path) as f:
        data = json.load(f)
    print(f"\n=== {label} ===")
    print(f"Timestamp: {data.get('timestamp')}")
    print(f"Model: {data.get('model')}")
    print(f"Latency: {data.get('latency_ms')}ms")
    print(f"Streaming: {data.get('streaming', 'N/A')}")

    req = data.get("request_body", {})
    if isinstance(req, dict):
        print(f"Request keys: {sorted(req.keys())}")
        print(f"Has stream param: {'stream' in req}")
        if "stream" in req:
            print(f"Stream value: {req['stream']}")
        print(f"anthropic_version: {req.get('anthropic_version', 'N/A')}")
        msgs = req.get("messages", [])
        print(f"Message count: {len(msgs)}")
        total = 0
        for m in msgs:
            total += len(str(m.get("content", "")))
        print(f"Total content chars: {total}")
        # Check max_tokens
        print(f"max_tokens: {req.get('max_tokens', 'N/A')}")
        # Check system prompt size
        sys_prompt = req.get("system", "")
        if isinstance(sys_prompt, list):
            sys_size = sum(len(str(s)) for s in sys_prompt)
        else:
            sys_size = len(str(sys_prompt))
        print(f"System prompt chars: {sys_size}")
        # Check tools
        tools = req.get("tools", [])
        print(f"Tools count: {len(tools)}")

    resp = data.get("response_body", {})
    if isinstance(resp, dict):
        print(f"Response keys: {list(resp.keys())[:10]}")
        usage = resp.get("usage", {})
        if usage:
            print(f"Usage: {usage}")
        output = resp.get("content", [])
        if output:
            out_chars = sum(len(str(c)) for c in output)
            print(f"Output content chars: {out_chars}")
