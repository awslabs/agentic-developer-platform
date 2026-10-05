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


def observe_identity_binding(*, route: str, outcome: str, enforced: bool) -> None:
    """Count one server-derived-identity decision on an internal-plane route (#5663).

    The rollout for A09 is "deploy in log-only mode, read the per-route deny
    counters for a full nightly cycle, then enforce". That is only possible if a
    would-be denial is countable while it is still being allowed, so ``outcome``
    and ``enforced`` are separate dimensions rather than one combined verdict:

        outcome="denied"  enforced=True   -> the request was refused
        outcome="denied"  enforced=False  -> log-only; it was ALLOWED but would be
                                             refused once the route is enforced.
                                             This is the number the rollout gate
                                             reads before flipping a route.
        outcome="allowed"                 -> the denominator, without which a zero
                                             deny count is indistinguishable from
                                             telemetry that never arrived.
        outcome="unresolved"              -> the server-owned record could not be
                                             read, so no comparison happened. Counted
                                             separately (#5663) because folding it
                                             into "allowed" would let a route that
                                             can no longer resolve ANY identity
                                             report a clean bill of health: a deny
                                             rate of zero over a denominator of
                                             vacuous checks. If this dominates, the
                                             control is not working even though
                                             nothing is being denied.

    Route is a static string chosen at the call site, never caller-supplied text —
    CloudWatch dimension values are billed per unique combination, so a
    caller-controlled dimension is a cost amplification primitive.

    Never raises: telemetry cannot be allowed to change an authorization outcome.
    """
    counts = {
        "InternalIdentityBindingChecked": 1,
        "InternalIdentityBindingDenied": int(outcome == "denied" and enforced),
        "InternalIdentityBindingWouldDeny": int(outcome == "denied" and not enforced),
        "InternalIdentityBindingUnresolved": int(outcome == "unresolved"),
    }
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "BedrockGateway",
                    "Dimensions": [["Environment", "Route"]],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in counts],
                }
            ],
        },
        "Environment": os.environ.get("BG_ENVIRONMENT", "dev"),
        "Route": route,
        **counts,
    }
    try:
        print(json.dumps(payload), flush=True)
    except Exception:
        logger.warning("Internal identity-binding observation could not be emitted for route=%s", route)
