"""Load a report renderer exclusively from trusted worker configuration."""

import importlib
import json
import os
import re


def report_renderer(persona, env=None):
    config = json.loads(
        (os.environ if env is None else env).get("ADP_TASK_REPORT_RENDERERS", "{}")
    )
    if not isinstance(config, dict):
        raise ValueError("Report renderer registry must be an object")
    target = config.get(persona)
    if target is None:
        return None
    if not isinstance(target, str) or not re.fullmatch(
        r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*", target
    ):
        raise ValueError("Invalid configured report renderer")
    module, name = target.split(":")
    try:
        renderer = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise ValueError("Configured report renderer is unavailable") from exc
    if not callable(renderer):
        raise ValueError("Configured report renderer is not callable")
    return renderer
