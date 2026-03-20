#!/usr/bin/env python3
import json

with open("/tmp/trace.json") as f:
    data = json.load(f)

for trace in data.get("Traces", []):
    print(f"Trace ID: {trace['Id']}")
    print(f"Duration: {trace.get('Duration', '?')}s")
    print()
    for seg in trace.get("Segments", []):
        doc = json.loads(seg["Document"])
        name = doc.get("name", "?")
        start = doc.get("start_time", 0)
        end = doc.get("end_time", 0)
        dur_ms = (end - start) * 1000
        http_req = doc.get("http", {}).get("request", {})
        http_resp = doc.get("http", {}).get("response", {})
        print(f"  Segment: {name}  ({dur_ms:.0f}ms)")
        if http_req.get("url"):
            url = http_req["url"]
            if len(url) > 80:
                url = url[:80] + "..."
            print(f"    URL: {url}")
        if http_resp.get("status"):
            print(f"    Status: {http_resp['status']}")
        for sub in doc.get("subsegments", []):
            sub_name = sub.get("name", "?")
            sub_start = sub.get("start_time", 0)
            sub_end = sub.get("end_time", 0)
            sub_dur = (sub_end - sub_start) * 1000
            print(f"    └─ {sub_name}: {sub_dur:.0f}ms")
            for subsub in sub.get("subsegments", []):
                ss_name = subsub.get("name", "?")
                ss_start = subsub.get("start_time", 0)
                ss_end = subsub.get("end_time", 0)
                ss_dur = (ss_end - ss_start) * 1000
                print(f"       └─ {ss_name}: {ss_dur:.0f}ms")
        print()
