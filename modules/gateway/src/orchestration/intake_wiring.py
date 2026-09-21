"""Deployment wiring for the intake conversation surface (#5331).

EPIC #4191. Constructs the `IntakeSessionReader` and `IntakeDispatcher` that
`intake_routes.py` depends on, from process environment that Terraform stamps in.

--------------------------------------------------------------------------------
Why this is a separate module from the routes and the logic
--------------------------------------------------------------------------------

`intake_session.py` and `intake_dispatch.py` take their store and queue client as
constructor arguments and never reach for AWS themselves, which is what lets their
ownership and FIFO-ordering rules be tested exhaustively offline. That property only
survives if exactly one module knows how to build the real clients — otherwise the
logic modules grow an `os.environ` read apiece and the tests start needing a mocked
environment to exercise an authorization rule.

So this module holds every environment name, every boto3 import and the process-wide
client cache; the routes import it lazily inside their dependency functions so that
importing the router does not import boto3, and so a test can override the dependency
without the real one ever being constructed.

--------------------------------------------------------------------------------
Unconfigured is a supported state, not an error
--------------------------------------------------------------------------------

Both builders return an object rather than raising when the environment is absent:
an unconfigured `IntakeSessionReader(None)` and an unconfigured
`IntakeDispatcher(None, "")`. Each then answers "unavailable" on every verb, which
the routes map to 503 and the CLI maps to its documented `unavailable` exit code.

Raising here instead would be worse in two distinct ways. At import time it would
take down a gateway that is otherwise healthy, so a deployment that merely cannot
*plan* would also stop proxying, approving gates and serving the dashboard. At call
time it would surface as a 500, telling an operator that something broke when the
true answer is that a function name was never set — a different action entirely.

The agent-factory stack publishes the resource names and grants the gateway
read access and permission to invoke intake. Apply that stack and deploy both
Lambda handlers before enabling the gateway surface. Missing configuration is
reported explicitly; code tests do not establish that a deployment is wired.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.orchestration.intake_dispatch import IntakeDispatcher
from src.orchestration.intake_session import IntakeSessionReader

logger = logging.getLogger(__name__)

# The DynamoDB table the agent-factory ingest and response Lambdas already write
# intake sessions to (`adp-<env>-agent-gateway-sessions`). Named by environment
# rather than derived from an env prefix here, because deriving it would encode a
# naming convention this module does not own and would fail silently — pointing at a
# nonexistent table looks exactly like a user with no sessions.
SESSIONS_TABLE_ENV = "BG_INTAKE_SESSIONS_TABLE"

# The agent-factory ingest Lambda (`adp-<env>-agent-gateway-ingest`). A FUNCTION
# NAME, not a queue URL: the turn enters through `handler.lambda_handler` so that it
# gets the session row, the thread, the transcript and the registered run a browser
# turn gets. See `intake_dispatch.py` on why writing to the queue directly produced a
# turn whose conversation did not exist.
#
# The previous `BG_INTAKE_QUEUE_URL` is deliberately NOT read as a fallback. A
# deployment still carrying only the old variable must report unavailable, not quietly
# resume the shortcut this change exists to remove.
INTAKE_FUNCTION_ENV = "BG_INTAKE_INGEST_FUNCTION"

# The chat-context table (`adp-<env>-chat-context`), where the worker's `DraftStore`
# keeps the live draft at `PK=session#<id>, SK=draft`. A SECOND table because the
# draft is genuinely not on the session row — see `intake_session.py`'s docstring.
#
# Separately optional from the sessions table on purpose: with this unset a
# conversation is still readable (transcript, issue, in-flight state) and only the
# draft comes back flagged unavailable. Folding the two together would make a missing
# context table 503 a readback that mostly works.
CONTEXT_TABLE_ENV = "BG_INTAKE_CONTEXT_TABLE"

_sessions_table: Any | None = None
_context_table: Any | None = None
_lambda_client: Any | None = None


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("BG_AWS_REGION") or "us-east-1"


def _get_sessions_table() -> Any | None:
    """The sessions table resource, or None when unconfigured.

    Cached process-wide, matching `dispatch_pass._get_sqs_client`: boto3 client
    construction is not free, and a request-scoped client would pay it on every
    poll of a conversation — which is precisely the call a watching CLI repeats.
    """
    global _sessions_table
    table_name = (os.environ.get(SESSIONS_TABLE_ENV) or "").strip()
    if not table_name:
        return None
    if _sessions_table is None:
        import boto3

        _sessions_table = boto3.resource("dynamodb", region_name=_region()).Table(table_name)
    return _sessions_table


def _get_context_table() -> Any | None:
    """The chat-context table resource, or None when unconfigured.

    Cached for the same reason as the sessions table: a watching CLI polls, and each
    poll now reads two tables.
    """
    global _context_table
    table_name = (os.environ.get(CONTEXT_TABLE_ENV) or "").strip()
    if not table_name:
        return None
    if _context_table is None:
        import boto3

        _context_table = boto3.resource("dynamodb", region_name=_region()).Table(table_name)
    return _context_table


def _get_lambda_client() -> Any | None:
    """The Lambda client used to invoke the ingest function.

    Configured with a read timeout that exceeds the ingest Lambda's own 30s ceiling.
    boto3's default is 60s, which is already sufficient, but it is pinned explicitly
    because the default changing would turn a slow cold start into a spurious dispatch
    failure — and the caller's retry would then land a SECOND turn in a conversation
    that had already accepted the first.
    """
    global _lambda_client
    if _lambda_client is None:
        import boto3
        from botocore.config import Config

        _lambda_client = boto3.client(
            "lambda",
            region_name=_region(),
            config=Config(read_timeout=60, connect_timeout=5, retries={"max_attempts": 0}),
        )
    return _lambda_client


def session_reader() -> IntakeSessionReader:
    """The reader for this deployment; unconfigured if the sessions table is not set.

    The context table is passed independently, so the two degrade independently.
    """
    return IntakeSessionReader(_get_sessions_table(), _get_context_table())


def dispatcher() -> IntakeDispatcher:
    """The dispatcher for this deployment; unconfigured if the function is not set."""
    function_name = (os.environ.get(INTAKE_FUNCTION_ENV) or "").strip()
    if not function_name:
        # No client is constructed at all when there is nowhere to send: building one
        # would make `is_configured` depend on two things that can disagree, and the
        # route needs a single honest answer to give the caller.
        return IntakeDispatcher(None, "")
    return IntakeDispatcher(_get_lambda_client(), function_name)


def reset_clients_for_testing() -> None:
    """Drop the cached clients.

    Exists so a test that changes the environment is not served a client built from
    the previous one — the cache is process-wide by design, and without this a
    configured-vs-unconfigured test would depend on execution order.
    """
    global _sessions_table, _context_table, _lambda_client
    _sessions_table = None
    _context_table = None
    _lambda_client = None
