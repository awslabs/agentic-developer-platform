"""Count every credential-binding decision, including zero-drift observations."""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)


def observe_binding(*, from_registry: bool, drift: bool) -> None:
    """Emit one coherent denominator/outcome sample without identity or secrets.

    Absence of drift events alone cannot prove that the guard ran. Explicit
    zeroes and the checked/from-registry counters let the rollout gate distinguish
    complete observations from absent or partially ingested telemetry.
    """
    counts = {
        "CredentialAuthorizationChecked": 1,
        "CredentialAuthorizationFromRegistry": int(from_registry),
        "CredentialAuthorizationDrift": int(drift),
        "CredentialAuthorizationFallback": int(not from_registry),
    }
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "BedrockGateway",
                    "Dimensions": [["Environment"]],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in counts],
                }
            ],
        },
        "Environment": os.environ.get("BG_ENVIRONMENT", "dev"),
        **counts,
    }
    try:
        print(json.dumps(payload), flush=True)
    except Exception:
        # A logging failure cannot authorize a request or change its refusal.
        logger.warning("Credential-binding observation could not be emitted")
