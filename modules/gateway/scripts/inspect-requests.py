#!/usr/bin/env python3
"""Inspect the 5 most recent chat log files."""
import json
import sys

files = [f"/tmp/req{i}.json" for i in range(1, 6)]

for f in files:
    try:
        with open(f) as fh:
            d = json.load(fh)
        
        # Extract key fields
        ts = d.get("timestamp", "?")
        model = d.get("model", "?")
        latency = d.get("latency_ms", "?")
        api_format = d.get("api_format", "?")
        usage = d.get("response", {}).get("usage", {})
        input_tok = usage.get("input_tokens", 0)
        output_tok = usage.get("output_tokens", 0)
        
        # Check if there's a user message we can peek at
        messages = d.get("request", {}).get("messages", [])
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_user_msg = block.get("text", "")[:100]
                            break
                elif isinstance(content, str):
                    last_user_msg = content[:100]
                break
        
        print(f"File: {f}")
        print(f"  Timestamp:    {ts}")
        print(f"  Model:        {model}")
        print(f"  Latency (ms): {latency}")
        print(f"  API format:   {api_format}")
        print(f"  Input tokens: {input_tok}")
        print(f"  Output tokens:{output_tok}")
        print(f"  Last user msg:{last_user_msg[:80]}")
        print()
    except Exception as e:
        print(f"Error reading {f}: {e}")
