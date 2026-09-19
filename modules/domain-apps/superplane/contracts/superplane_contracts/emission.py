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
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from .connections import ConnectionState, ValidationReport
from .secrets import (
    find_secret_material,
    key_names_secret,
    redact_secret_spans,
    value_is_secret_shaped,
)

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

    THE SUPPORTED CONTAINER AND OBJECT CONTRACT
    -------------------------------------------
    Stated explicitly, because the previous version of this function was named as
    though it redacted universally and did not. Issue #5053's review found five
    live escape routes, every one of which reached a handler with a working AWS key
    or a complete secret ARN in it. The name of a helper is not evidence of its
    coverage, so the coverage is written down here and asserted by tests:

    * **Mappings** — any :class:`collections.abc.Mapping`, not only ``dict``.
      ``UserDict``, ``MappingProxyType`` (what ``vars(obj)`` returns) and any
      third-party mapping are traversed. Previously only a literal ``dict`` was,
      so ``logger.info("%s", vars(connection))`` leaked.
    * **Sequences** — ``list`` and ``tuple``, and any other non-string
      :class:`~collections.abc.Sequence` (rendered as a list, since an arbitrary
      sequence type cannot be reliably reconstructed from its items).
    * **Sets** — ``set`` and ``frozenset`` and any other
      :class:`~collections.abc.Set`. Returned as a ``set`` of scrubbed members.
      Unordered containers were skipped entirely before, so a bare
      ``{"AKIA..."}`` passed through verbatim.
    * **Strings and bytes** are scalars here, never sequences to recurse into.
      Treating a ``str`` as a sequence would recurse per character and never match
      a multi-character secret shape.
    * **Everything else** — any object that is not one of the above — is rendered
      with both ``repr()`` and ``str()`` and those renderings are checked. This is the case no container
      rule can reach: a dataclass or an exception whose ``repr`` embeds a key was
      emitted verbatim, because the object itself is not secret-*shaped* and was
      returned untouched for the handler to format later. If the rendering carries
      secret material the whole object becomes ``PLACEHOLDER``; otherwise the
      original object is returned so a handler still formats the real value and
      quota/capacity numbers keep their types.

    Non-string scalars still pass through unchanged when their rendering is clean:
    rewriting them would corrupt the quota and capacity numbers this contract
    exists to report.

    Cycles terminate. A self-referencing container is rendered as
    ``RECURSION_PLACEHOLDER`` on the second visit rather than recursing forever,
    and depth is bounded by ``_MAX_DEPTH``. A log line must not be able to hang
    the process that writes it, and a hostile payload is exactly where that would
    otherwise happen.
    """
    return _scrub(value, _seen=frozenset(), _depth=0)


def redact_spans(text: str) -> str:
    """Replace only the secret-shaped *spans* of a message, keeping the rest.

    Issue #5053 (U7b). ``scrub`` is the right tool for a value of unknown
    provenance: it withholds the whole thing, because any part of it might be the
    secret. That is the wrong tool for a *validation message*, which is deliberately
    written prose explaining which rule a field broke. Replacing it wholesale turns
    "adp_credential_id must not be an ARN, store the opaque credential id instead"
    into ``[REDACTED]``, and the caller can no longer tell a rejected ARN from a
    rejected empty string. A refusal that cannot be acted on is its own defect.

    So this redacts spans rather than values: each known secret pattern and each ARN
    prefix match is replaced in place and the surrounding explanation survives.

    It is **not** a replacement for ``scrub`` and must not be used on arbitrary
    values. It assumes its input is a message whose non-matching text is safe to
    show, which is true of a validator's own prose and not true of a payload field.
    Where that assumption does not hold, withhold the whole value.

    The matching itself lives in ``secrets.py`` beside the patterns, so this and
    ``value_is_secret_shaped`` cannot come to disagree about what a secret looks like.
    """
    return redact_secret_spans(text, placeholder=PLACEHOLDER)


# Written in place of a container that contains itself, and of anything nested
# deeper than `_MAX_DEPTH`. Distinct from PLACEHOLDER so an operator reading a log
# can tell "withheld because secret" from "not rendered because pathological".
RECURSION_PLACEHOLDER = "[RECURSION]"
DEPTH_PLACEHOLDER = "[TRUNCATED]"

# Deep enough for any legitimate payload this contract carries, shallow enough
# that a hostile one cannot exhaust the interpreter stack inside a log call.
_MAX_DEPTH = 24


def _scrub_rendered(value: Any) -> Any:
    """Check both renderings of an object and withhold it if either leaks.

    The object is returned unchanged when its rendering is clean, so a handler
    still formats the real value. Only a rendering that carries secret material
    replaces the object, because the *rendering* is what reaches the log.

    A rendering that raises is itself withheld: an object whose rendering cannot be
    inspected cannot be shown to be safe, and guessing in the permissive direction
    here is what this function exists to prevent.
    """
    try:
        rendered = (repr(value), str(value))
    except Exception:  # noqa: BLE001 - see below; narrowing this would reopen the leak
        # Deliberately blind. Rendering runs arbitrary third-party code and may raise
        # literally any exception type, including ones defined by a provider SDK.
        # Catching a narrower set would let an unanticipated type propagate out of a
        # *log call*, turning a redaction step into an outage; and returning the
        # object unexamined would emit a rendering nobody has checked. Withholding
        # is the only answer that is safe in both directions.
        return PLACEHOLDER
    if any(find_secret_material(text) is not None for text in rendered):
        return PLACEHOLDER
    return value


def _key_names_secret(key: Any) -> bool:
    try:
        name = (
            bytes(key).decode("utf-8", errors="replace")
            if isinstance(key, (bytes, bytearray))
            else str(key)
        )
    except Exception:
        return True
    return key_names_secret(name)


def _scrub(value: Any, *, _seen: frozenset[int], _depth: int) -> Any:
    if _depth > _MAX_DEPTH:
        return DEPTH_PLACEHOLDER

    # Scalars first. `str`/`bytes` are Sequences, so they must be settled before
    # any Sequence branch, and they are the common case besides.
    if isinstance(value, str):
        return PLACEHOLDER if value_is_secret_shaped(value) else value
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="replace")
        return PLACEHOLDER if value_is_secret_shaped(text) else value
    if value is None or isinstance(value, (bool, int, float, complex)):
        return value

    if isinstance(value, Mapping):
        if id(value) in _seen:
            return RECURSION_PLACEHOLDER
        seen = _seen | {id(value)}
        out: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = _scrub_rendered(key)
            # Distinct secret-bearing keys can collapse onto the same placeholder.
            # Neither input order nor an existing placeholder may restore a value.
            if safe_key in out or _key_names_secret(key):
                out[safe_key] = PLACEHOLDER
            else:
                out[safe_key] = _scrub(item, _seen=seen, _depth=_depth + 1)
        return out

    if isinstance(value, AbstractSet):
        if id(value) in _seen:
            return RECURSION_PLACEHOLDER
        seen = _seen | {id(value)}
        # Returned as a plain `set`: an arbitrary Set implementation cannot be
        # reconstructed from its members in general, and a redacted member may not
        # be hashable-compatible with the original type's invariants.
        return {_scrub(item, _seen=seen, _depth=_depth + 1) for item in value}

    if isinstance(value, (list, tuple)):
        if id(value) in _seen:
            return RECURSION_PLACEHOLDER
        seen = _seen | {id(value)}
        scrubbed = [_scrub(item, _seen=seen, _depth=_depth + 1) for item in value]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed

    if isinstance(value, Sequence):
        if id(value) in _seen:
            return RECURSION_PLACEHOLDER
        seen = _seen | {id(value)}
        # Rendered as a list for the same reason sets are rendered as sets: the
        # concrete type's constructor is not knowable here.
        return [_scrub(item, _seen=seen, _depth=_depth + 1) for item in value]

    # Unknown objects can be rendered by either string or repr formatters.
    return _scrub_rendered(value)


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
        extras = {
            key: value for key, value in record.__dict__.items() if key not in standard
        }
        for key in extras:
            del record.__dict__[key]
        record.__dict__.update(scrub(extras))
        if isinstance(record.msg, str):
            scrubbed = scrub(record.msg)
            if scrubbed != record.msg:
                record.msg = scrubbed
        elif record.msg is not None:
            record.msg = scrub(record.msg)

        # A withheld template has no interpolation slots. Keeping its old
        # arguments would make handlers drop the record with a formatting error.
        if isinstance(record.msg, str) and record.msg == PLACEHOLDER:
            record.args = ()

        if record.args:
            # Scrubbed with one unconditional call rather than a type switch.
            #
            # `record.args` is NOT always a tuple. `logging` has a documented
            # special case: a single argument that is a non-empty Mapping becomes
            # `record.args` *itself*, so `%(key)s` templates work. The previous
            # `isinstance(..., dict) / isinstance(..., tuple)` pair therefore
            # matched NEITHER branch for `logger.info("%s", UserDict(...))` and
            # the mapping reached the handler unredacted -- the same class of gap
            # as `scrub` skipping non-dict Mappings, one layer up, and invisible
            # to any test that only called `scrub` directly.
            #
            # `scrub` already dispatches on Mapping/Set/Sequence and preserves
            # tuple-ness, so delegating to it covers the mapping case, the tuple
            # case and any future args type, and keeps one definition of what a
            # secret is.
            record.args = scrub(record.args)
        # Formatting can assemble a secret from harmless fragments or an object's
        # __str__ even when its repr is clean. Check what the handler will emit.
        # Keep clean argument types for structured/server formatters.
        try:
            rendered = record.getMessage()
        except Exception:
            # Rendering normally happens inside Handler.emit's error boundary.
            # Inspecting it here must not turn a malformed log into an app error.
            record.msg = PLACEHOLDER
            record.args = ()
        else:
            if scrub(rendered) != rendered:
                record.msg = PLACEHOLDER
                record.args = ()
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
