"""The way out: response bodies and log lines that cannot carry a value or an ARN.

Issue #5047 (U7), EPIC #4910. R7 acceptance 4.

## Why the log needs its own guard

Acceptance 4 names two surfaces — "no response body or log line" — and it is the
second that historically leaks. A response body is designed: somebody chose its
fields, and a reviewer can read the model and see that no secret field is in it.
A log line is incidental. It is written at 2am during an incident, it interpolates
whatever object is in scope, and `logger.info("connection %s", payload)` will
happily render a dict a reviewer never inspected. The response model being clean is
therefore no evidence at all about the log.

Worse, the log's blast radius is larger. A response body goes to one caller; a log
line goes to CloudWatch, to whatever aggregator ships it onward, and — for an agent
surface — into the run transcript and the model's context. R7 calls this
unrecoverable, and it is: rotating the credential does not retract the string, and
an ARN usually survives rotation unchanged, so a leaked pointer stays valid.

So there are two mechanisms here, not one:

* `connection_response()` builds the response body from an allowlist. It cannot
  leak a field nobody chose, because it does not copy fields — it names them.
* `SecretRedactingFilter` is a `logging.Filter` that scrubs records on the way to
  the handler, catching the interpolated-object case that no amount of care in a
  response model can reach.

## Allowlist rather than exclusion

`connection_response()` names the fields it emits. The alternative — take the
state's `__dict__` and delete the dangerous keys — is the arrangement that breaks
silently: a field added to `ConnectionState` later is emitted by default, and only
a reviewer noticing the new key keeps it out. With an allowlist, a new field is
absent until someone decides it may be published. The default is the safe one.

Note what is therefore *not* in the response: no ARN (never in the contract at
all), and no `detail` string from a provider is copied verbatim without passing the
secret check first.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from .connections import ConnectionState, ValidationReport
from .secrets import find_secret_material, key_names_secret, value_is_secret_shaped

# Written in place of anything withheld. Uniform, because a per-type placeholder
# ("[REDACTED AWS KEY]") would leak the kind of credential that was present.
PLACEHOLDER = "[REDACTED]"


def validation_response(report: ValidationReport) -> dict[str, Any]:
    """Serialize a validation report with its four readings kept separate.

    The four fields are emitted as four fields. No aggregate is computed here —
    acceptance 3's separation would be undone at the wire boundary if this returned
    a single `ok`, since every consumer reads the wire form rather than the Python
    object.

    `observed_capacity` stays `None` when unmeasured rather than becoming `0`:
    "we did not look" and "there is nothing free" are different operational facts
    and a consumer needs to tell them apart.
    """
    detail = report.detail
    if find_secret_material(detail) is not None:
        detail = PLACEHOLDER
    return {
        "credential_valid": report.credential_valid,
        "permissions_sufficient": report.permissions_sufficient,
        "quota_available": report.quota_available,
        "observed_capacity": report.observed_capacity,
        "checked_at": report.checked_at.isoformat(),
        "detail": detail,
    }


def connection_response(state: ConnectionState) -> dict[str, Any]:
    """Build the response body for a connection, from an allowlist of fields.

    Emits the credential **id** and never a value or an ARN. `credential_id` is the
    vault's opaque identifier and is safe to publish — it is not usable to read the
    secret, which is exactly why the contract carries it instead of an ARN. That
    distinction is enforced at construction by `CredentialReference`, so a value
    reaching this function cannot be an ARN in the first place.
    """
    body: dict[str, Any] = {
        "connection_id": state.connection_id,
        "provider": state.provider,
        "status": str(state.status),
        "workspace_id": state.workspace_id,
        "credential": {
            "credential_id": state.reference.credential_id,
            "service": state.reference.service,
            "label": state.reference.label,
        },
        "binding": {
            "credential_id": state.binding.credential_id,
            "workspace_id": state.binding.workspace_id,
            "bound_by": state.binding.bound_by,
            "bound_at": state.binding.bound_at.isoformat(),
        },
        "admits_new_work": state.admits_new_work(),
        "allows_renewal": state.allows_renewal(),
    }
    if state.validation is not None:
        body["validation"] = validation_response(state.validation)
    if state.limitation:
        # Surfaced in the response, not only in a log — acceptance 5's "surfaces the
        # limitation" is about what the operator who disabled the connection sees.
        body["limitation"] = state.limitation
    return body


def scrub(value: Any) -> Any:
    """Return `value` with secret material replaced by `PLACEHOLDER`.

    Recurses through mappings and sequences. Non-string scalars pass through: they
    cannot carry a secret shape, and rewriting them would corrupt the quota and
    capacity numbers this contract exists to report.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = PLACEHOLDER if value_is_secret_shaped(key) else key
            if isinstance(key, str) and key_names_secret(key):
                out[safe_key] = PLACEHOLDER
            else:
                out[safe_key] = scrub(item)
        return out
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub(item) for item in value)
    if value_is_secret_shaped(value):
        return PLACEHOLDER
    return value


class SecretRedactingFilter(logging.Filter):
    """A `logging.Filter` that scrubs secret material out of records.

    Attached to configured emission handlers as well as the logger, because the call
    sites are the problem: `logger.info("state=%s", payload)` is written by whoever
    is debugging, and no review process catches every one of them. Logger filters do not cover propagated child records; handler filters do.

    Both the message template and the interpolation arguments are scrubbed. Scrubbing
    only `record.getMessage()` would be the common half-fix — it renders the message
    once for inspection while the handler renders it again from the untouched
    `args`, so the scrub applies to a copy nobody emits.

    Returns True always: this filter redacts, it does not drop records. Dropping
    would mean an incident-time log line vanishes because it happened to contain a
    long hex string, which trades a disclosure for a blind spot.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not super().filter(record):
            return True
        if record.exc_info:
            record.exc_text = scrub(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        standard = logging.makeLogRecord({}).__dict__
        for key in list(record.__dict__):
            if key not in standard:
                if value_is_secret_shaped(key):
                    record.__dict__.pop(key)
                    record.__dict__[PLACEHOLDER] = PLACEHOLDER
                    continue
                record.__dict__[key] = (
                    PLACEHOLDER
                    if key_names_secret(key)
                    else scrub(record.__dict__[key])
                )
        if isinstance(record.msg, str):
            scrubbed = scrub(record.msg)
            if scrubbed != record.msg:
                record.msg = scrubbed
        elif record.msg is not None:
            record.msg = scrub(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = scrub(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(scrub(arg) for arg in record.args)
        return True


def install_log_redaction(logger: logging.Logger) -> SecretRedactingFilter:
    """Redact this logger and its subtree at all currently configured output handlers.

    Call after configuring handlers, and again after adding/replacing handlers.
    Only this logger's namespace is affected on shared ancestor handlers. Installing
    on the root logger covers all logger names, including propagated child records.
    """
    installed = next(
        (f for f in logger.filters if isinstance(f, SecretRedactingFilter)), None
    )
    if installed is None:
        installed = SecretRedactingFilter(
            "" if logger is logging.getLogger() else logger.name
        )
        logger.addFilter(installed)
    current = logger
    while current is not None:
        for handler in current.handlers:
            handler.addFilter(installed)
        if not current.propagate:
            break
        current = current.parent
    return installed
