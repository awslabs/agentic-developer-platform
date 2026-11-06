"""Private code locations for refused paid calls; no exception text or locals."""

import json
import logging
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)


def record(error):
    frames = [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)[-16:]
    ]
    logger.warning("%s", json.dumps({"event": "paid_provider_preflight_refused", "exception_type": type(error).__name__, "code_frames": frames}))
