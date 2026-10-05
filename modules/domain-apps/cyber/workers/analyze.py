"""Entered only after isolation.py installs the kernel sandbox."""
import json
from pathlib import Path
import sys


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    mode, sample, options = sys.argv[1:]
    body = json.loads(Path(options).read_text())
    if mode == "triage":
        from triage.handler import _fingerprint
        result = _fingerprint(Path(sample))
    elif mode == "static":
        from static.handler import _run_mode_a
        result = _run_mode_a(Path(sample), body.get("focus"), body.get("yara_rules"))
    else:
        raise ValueError("invalid analysis mode")
    print(json.dumps(result))
