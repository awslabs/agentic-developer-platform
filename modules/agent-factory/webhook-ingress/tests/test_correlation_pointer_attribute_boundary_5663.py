"""The worker credential cannot write lineage-authority attributes — #5663 (A09).

Finding ``f-7c46ead6``, the IAM half.

``CorrelationUpdates`` granted ``dynamodb:UpdateItem`` on the whole
``adp-*-correlation-pointers`` table with **no Condition**, so the agent-worker role
could set any attribute on any ``channel_key`` — including ``root_human_id``,
``is_human_rooted`` and ``chain_depth``, the three that decide whose authority a run
holds. #4129 removed those from the Python writer's *signature*, which is a code
property: it holds until someone adds a parameter back, and it holds not at all for a
pod that has been induced to call boto3 directly. The credential could still do it.

This module asserts the credential itself is bounded, via
``dynamodb:Attributes``/``ForAllValues:StringEquals`` — DynamoDB rejects the whole
UpdateItem when the expression names an attribute outside the allow-list.

WHY THIS IS NOT A POLICY-SHAPED STRING ASSERTION. Two independent halves:

  * the policy is rendered by real ``terraform console`` from the real
    ``agent-authority-boundary.tf`` (see ``render_worker_boundary.py``), not pattern-
    matched out of the HCL;
  * the allow-list is checked against the attribute names the REAL WRITERS actually
    send, parsed out of their source. A boundary narrower than its writers fails
    closed *silently* here, because every correlation-pointer write in this system is
    fail-soft (``except Exception`` / ``.catch``), so an ``AccessDeniedException``
    would be swallowed and simply stop lineage from connecting. That direction is the
    one a string assertion cannot catch, and it is the more dangerous one.

Scope: source-only. No IAM is applied and no live call is made.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PY_WRITER = (
    _REPO_ROOT / "modules/agent-factory/agent-worker-image/lib/correlation_store.py"
)
_TS_WRITER = (
    _REPO_ROOT / "modules/agent-factory/agent/src/lib/correlationStore.ts"
)

# The three attributes that carry lineage authority. The credential must not be able
# to write any of them, no matter what the pod's code does.
AUTHORITY_ATTRIBUTES = ("root_human_id", "is_human_rooted", "chain_depth")


@pytest.fixture(scope="module")
def statements():
    if shutil.which("terraform") is None:
        pytest.skip("Terraform is needed to render policy expressions")
    result = subprocess.check_output(
        [sys.executable, str(Path(__file__).with_name("render_worker_boundary.py"))],
        text=True,
    )
    return json.loads(result)["Statement"]


@pytest.fixture(scope="module")
def correlation_updates(statements):
    matches = [s for s in statements if s.get("Sid") == "CorrelationUpdates"]
    assert len(matches) == 1, "expected exactly one CorrelationUpdates statement"
    return matches[0]


@pytest.fixture(scope="module")
def allowed_attributes(correlation_updates):
    condition = correlation_updates.get("Condition")
    assert condition, "CorrelationUpdates must carry a Condition — an unconditioned UpdateItem grant lets the worker set any attribute, which is the finding"
    attrs = condition["ForAllValues:StringEquals"]["dynamodb:Attributes"]
    # ForAllValues is required: with plain StringEquals, a request naming SEVERAL
    # attributes is authorized if ANY one of them matches, which would not bound
    # anything.
    assert "ForAllValues:StringEquals" in condition
    return set(attrs)


# =============================================================================
# The escalation
# =============================================================================


def test_the_authority_attributes_are_not_writable(allowed_attributes):
    """The whole point: these three must never appear in the allow-list."""
    for attribute in AUTHORITY_ATTRIBUTES:
        assert attribute not in allowed_attributes, (
            f"{attribute!r} is writable by the worker credential — a pod could set it "
            "directly and launder its own authority, regardless of #4129's code change"
        )


def test_the_grant_is_still_updateitem_only_on_the_one_table(correlation_updates):
    """The narrowing must not have widened anything else by accident."""
    assert correlation_updates["Action"] == ["dynamodb:UpdateItem"]
    assert correlation_updates["Resource"].endswith("table/adp-dev-correlation-pointers")
    assert "PutItem" not in json.dumps(correlation_updates), (
        "PutItem replaces the whole item, which would clobber server-written "
        "attributes and bypass the per-attribute bound entirely (#1716/#4028)"
    )


# =============================================================================
# The regression half — the boundary must match the writers that EXIST
# =============================================================================


def _python_writer_attributes() -> set[str]:
    """Attribute names the Python worker writer sends, parsed from its source.

    Reads the ``"<name> = :<placeholder>"`` fragments it appends to ``set_parts``,
    plus the ``Key=`` attribute. Deliberately source-derived rather than hardcoded: if
    that writer gains an attribute, this test must notice and fail rather than let the
    boundary silently start refusing its writes.
    """
    source = _PY_WRITER.read_text()
    attrs = set(re.findall(r'"(\w+) = :\w+"', source))
    attrs |= set(re.findall(r'Key=\{"(\w+)":', source))
    assert attrs, "failed to parse any attribute names out of the Python writer"
    return attrs


def _ts_writer_attributes() -> set[str]:
    """Attribute names the Node worker writer sends, parsed from its source."""
    source = _TS_WRITER.read_text()
    expressions = re.findall(r"UpdateExpression:\s*'SET ([^']+)'", source)
    assert expressions, "failed to find the Node writer's UpdateExpression"
    attrs = {
        assignment.strip().split("=")[0].strip()
        for expression in expressions
        for assignment in expression.split(",")
    }
    attrs |= set(re.findall(r"Key:\s*\{\s*(\w+):", source))
    return attrs


def test_every_attribute_the_python_writer_sends_is_permitted(allowed_attributes):
    """Otherwise the boundary breaks lineage, and does it silently.

    ``lib/correlation_store.py`` wraps its ``update_item`` in ``except Exception ->
    logger.warning``, so an AccessDeniedException here is invisible: the pointer just
    never gets written and #1828 cross-channel lineage stops connecting.
    """
    missing = _python_writer_attributes() - allowed_attributes
    assert not missing, (
        f"the Python worker writer sends {sorted(missing)}, which the boundary "
        "refuses — this fails CLOSED and SILENTLY (the write is fail-soft)"
    )


def test_every_attribute_the_node_writer_sends_is_permitted(allowed_attributes):
    """Same for the TypeScript writer, whose failure is equally silent (``.catch``).

    Note this is why the unread ``latest_*`` names are in the allow-list. They confer
    no authority — no consumer reads them (the Lambda's reader reads
    ``correlation_id``/``root_human_id``/``is_human_rooted``) — but they ARE sent
    today, so excluding them would break a live writer rather than harden anything.
    """
    missing = _ts_writer_attributes() - allowed_attributes
    assert not missing, (
        f"the Node worker writer sends {sorted(missing)}, which the boundary refuses"
    )


def test_the_allow_list_has_no_attribute_no_writer_sends(allowed_attributes):
    """Keeps the list honest in the other direction.

    An allow-list that drifts wider than its writers is how an unconditioned grant
    grows back one attribute at a time.
    """
    unused = allowed_attributes - (_python_writer_attributes() | _ts_writer_attributes())
    assert not unused, f"allow-list permits {sorted(unused)}, which no writer sends"


def test_the_node_writer_does_not_send_the_bare_authority_names(allowed_attributes):
    """A guard on the reconciliation that is still outstanding.

    The TS writer sends ``latest_root_human_id`` / ``latest_is_human_rooted``, which
    nothing reads. If it is ever "fixed" by renaming those to the bare names the
    reader consults, it would start writing real authority fields — and this boundary
    would then refuse them, silently. That follow-up must change BOTH sides; this test
    is what makes the coupling fail loudly instead.
    """
    sent = _ts_writer_attributes()
    for attribute in AUTHORITY_ATTRIBUTES:
        assert attribute not in sent, (
            f"the Node writer now sends {attribute!r}; reconcile the writer and this "
            "boundary together (see #4129 — never applied to the TS path)"
        )
