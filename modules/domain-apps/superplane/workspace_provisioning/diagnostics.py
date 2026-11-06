"""Bounded failure locations, without provider messages, bodies, or credentials."""

import json
from pathlib import Path
import sys
import traceback


def report_failure(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        chain.append(
            {
                "exception_type": type(error).__name__,
                "frames": [
                    {
                        "file": Path(frame.filename).name,
                        "line": frame.lineno,
                        "function": frame.name,
                    }
                    for frame in traceback.extract_tb(error.__traceback__)[-12:]
                ],
            }
        )
        error = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
    print(
        json.dumps({"event": "lifecycle_provider_failed", "exceptions": chain}),
        file=sys.stderr,
    )


def diagnostic_hook(hook):
    async def call(intent):
        try:
            return await hook(intent)
        except Exception as error:
            report_failure(error)
            raise

    return call
