"""The agent pod cannot write chain provenance to its pointer row — Issue #4129.

This pod holds ``dynamodb:UpdateItem`` on ``adp-*-correlation-pointers``. Three
attributes on that row decide whose authority a spawned run holds and how deep
the chain has gone: ``root_human_id``, ``is_human_rooted``, ``chain_depth``. A
compromised run could set ``root_human_id=<victim> is_human_rooted=true
chain_depth=0``, trigger the channel, and have the webhook persist the victim's
id as a *legitimate server-side* ``authorized_user_id`` with the depth counter
reset — a cross-tenant escalation laundered through one DDB row.

The webhook now derives all three from the ``correlation-index`` GSI on
``webhook-events`` (server-written only), so this side sends none of them.

Two levels are asserted, because only the pair is durable:

  - **Wire level** — the ``UpdateExpression`` contains none of the three names.
    This is the property IAM will pin in the follow-up PR that adds the
    ``dynamodb:Attributes`` Condition; if this drifts, that Condition starts
    denying every pointer write instead of just the forbidden ones. Note the
    Condition deliberately does NOT land in the same PR: ``dynamodb:Attributes``
    is all-or-nothing per request and ``write_pointer`` is fail-soft, so
    tightening IAM before this image is live would silently degrade every chain
    to new-chain-per-event with nothing raising.

  - **Signature level** — the parameters are GONE, not accepted-and-ignored, so a
    future caller passing them is a ``TypeError`` at the call site rather than a
    silent no-op that reads as if it worked.

CI path: ``.github/workflows/provenance-contract-tests.yml`` (worker-producer
job) — the ``agent-worker-image/tests/`` suite is in no other workflow, so this
file is named explicitly there alongside its ``lib/`` and ``entrypoint.py``
paths. It is NOT covered by ``webhook-ingress-ci.yml``, which only runs
``pytest lambda/``.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import correlation_store  # noqa: E402

FORBIDDEN = ("root_human_id", "is_human_rooted", "chain_depth")


@pytest.fixture(autouse=True)
def _reset_client():
    correlation_store._ddb = None
    yield
    correlation_store._ddb = None


def _write(**kwargs) -> dict:
    """Call write_pointer against a mock DDB client and return the update_item kwargs."""
    client = MagicMock()
    with patch("lib.correlation_store._get_client", return_value=client):
        with patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"}):
            correlation_store.write_pointer(**kwargs)
    client.update_item.assert_called_once()
    return client.update_item.call_args[1]


class TestWireFormatCarriesNoProvenance:
    """What actually reaches DynamoDB."""

    @pytest.mark.parametrize("attr", FORBIDDEN)
    def test_update_expression_omits_the_attribute(self, attr):
        call = _write(
            channel_key="github:repo=aws-e/adp,issue=783",
            correlation_id="corr-1",
            triggering_invocation_id="inv-1",
            last_triggered_persona="developer",
        )
        assert attr not in call["UpdateExpression"]

    def test_no_expression_value_smuggles_provenance(self):
        """Belt-and-braces: the values map holds only owned data.

        A placeholder such as ``:rh`` present but unreferenced would pass the
        name check above while still shipping the value the day someone adds the
        SET clause back.
        """
        call = _write(channel_key="k", correlation_id="corr-1")
        assert set(call["ExpressionAttributeValues"]) <= {":cid", ":ua", ":ea", ":tii", ":ltp"}

    def test_attributes_the_pod_does_own_are_still_written(self):
        """The narrowing must not take lineage or the guard down with it.

        ``correlation_id`` and ``triggering_invocation_id`` carry the chain and
        the parent edge (neither grants authority); ``last_triggered_persona`` is
        the #1716/#2149 self-re-trigger guard. Dropping any of these would break
        #1828 cross-issue dispatch — fail-soft, so with no error surfaced.
        """
        call = _write(
            channel_key="github:repo=aws-e/adp,issue=783",
            correlation_id="corr-1",
            triggering_invocation_id="inv-1",
            last_triggered_persona="reviewer",
        )
        expr, vals = call["UpdateExpression"], call["ExpressionAttributeValues"]
        assert "correlation_id = :cid" in expr
        assert "triggering_invocation_id = :tii" in expr
        assert "last_triggered_persona = :ltp" in expr
        assert "expires_at = :ea" in expr  # TTL still set
        assert vals[":cid"] == {"S": "corr-1"}
        assert vals[":tii"] == {"S": "inv-1"}
        assert vals[":ltp"] == {"S": "reviewer"}


class TestSignatureRefusesProvenance:
    """Removed, not accepted-and-ignored."""

    @pytest.mark.parametrize("attr", FORBIDDEN)
    def test_parameter_is_absent_from_the_signature(self, attr):
        assert attr not in inspect.signature(correlation_store.write_pointer).parameters

    @pytest.mark.parametrize("attr", FORBIDDEN)
    def test_passing_the_parameter_raises(self, attr):
        """A reintroduced call site fails loudly instead of no-op'ing.

        Silence is the dangerous outcome here: this function is fail-soft, so an
        ignored kwarg would let a caller believe it had set provenance while the
        chain quietly resolved elsewhere.
        """
        with pytest.raises(TypeError):
            correlation_store.write_pointer(
                channel_key="k", correlation_id="corr-1", **{attr: "x"}
            )


class TestNoCallerStillPassesProvenance:
    """No in-tree caller passes the removed kwargs.

    A static check, because the ``TypeError`` above only fires on a path a test
    actually executes — ``seed_trigger_pointer`` and ``entrypoint`` each call
    ``write_pointer`` from a branch unit tests reach only with the right env set.

    AST rather than grep: ``entrypoint.py`` legitimately still keeps
    ``root``/``rooted`` locals for the gateway ``post_provenance`` call, so a
    substring search over the file would either false-positive on that or need an
    exemption broad enough to miss a real regression. This inspects the keywords
    of ``write_pointer`` call nodes only.
    """

    CALLERS = ["lib/seed_trigger_pointer.py", "entrypoint.py"]

    @staticmethod
    def _write_pointer_call_keywords(relpath: str) -> list[set[str]]:
        import ast

        tree = ast.parse((Path(__file__).resolve().parent.parent / relpath).read_text())
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "write_pointer":
                calls.append({kw.arg for kw in node.keywords if kw.arg})
        return calls

    @pytest.mark.parametrize("relpath", CALLERS)
    def test_caller_passes_no_provenance_kwarg(self, relpath):
        calls = self._write_pointer_call_keywords(relpath)
        assert calls, f"expected at least one write_pointer call in {relpath}"
        for kwargs in calls:
            assert not (kwargs & set(FORBIDDEN)), (
                f"{relpath} passes {sorted(kwargs & set(FORBIDDEN))} to write_pointer"
            )

    @pytest.mark.parametrize("relpath", CALLERS)
    def test_caller_uses_kwargs_only(self, relpath):
        """Positional args would let provenance slip past the kwarg check above."""
        import ast

        tree = ast.parse((Path(__file__).resolve().parent.parent / relpath).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "write_pointer":
                    assert not node.args, f"{relpath} calls write_pointer positionally"

    @pytest.mark.parametrize(
        "var", ["ADP_ROOT_HUMAN_ID", "ADP_IS_HUMAN_ROOTED", "ADP_CHAIN_DEPTH"]
    )
    def test_seed_trigger_pointer_reads_no_provenance_env(self, var):
        """#1828 seeding no longer sources ADP_ROOT_HUMAN_ID / ADP_CHAIN_DEPTH.

        Those env vars come from the pod's own environment, so reading them back
        and writing them onto the pointer row IS the laundering hop — the pod
        attesting to its own authority. The gate is now ``correlation_id`` alone.

        Compares against ``ast.unparse`` output, not the raw file: the docstring
        and comments explaining this removal necessarily name the variables, so a
        raw substring search fails on the very code it is meant to approve.
        """
        import ast

        path = Path(__file__).resolve().parent.parent / "lib/seed_trigger_pointer.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):  # drop docstrings, which survive unparse
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(
                node
            ):
                node.body = node.body[1:]
        assert var not in ast.unparse(tree)
