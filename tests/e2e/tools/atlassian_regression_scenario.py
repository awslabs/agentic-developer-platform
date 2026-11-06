"""Execute legacy connector checks and independently inspect their observations.

The baseline is copied from the accepted, pre-cutover source below, never from
the candidate's tools/list. Observations belong in the private evidence archive.
Rechecking a transcript calls no provider and cannot repeat a recorded write.
This module does not convert older, unqualified evidence into a live pass.
"""

from copy import deepcopy
import hashlib
import re
from uuid import UUID

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.github_integration import github_ac04
from tests.e2e.tools.jira_integration import jira_ac04
from tests.e2e.tools.slack_integration import slack_ac04
from tests.e2e.tools.scenarios import FixtureUnavailable


BASELINE_REVISION = "3ad38d178902805e5f0043445eb12520921a5b55"
BASELINE_FILE_SHA256 = (
    "2e4d7de1136e39c8aab7f999dbbca7f6a80381e7afdee70bc87450b158b480ea"
)
BASELINE_SHA256 = "4a365f4f39d87a8629435c1a7510e8cc6b74eb9638790360ecb77d63528ff9b9"
BASELINE = {
    "github___get_user": {
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "scope": "read:user",
    },
    "jira___get_issue": {
        "inputSchema": {
            "type": "object",
            "properties": {
                "cloudId": {"type": "string"},
                "issueIdOrKey": {"type": "string"},
            },
            "required": ["cloudId", "issueIdOrKey"],
            "additionalProperties": False,
        },
        "scope": "read:jira-work",
    },
    "slack___get_user": {
        "inputSchema": {
            "type": "object",
            "properties": {"user": {"type": "string"}},
            "required": ["user"],
            "additionalProperties": False,
        },
        "scope": "users:read",
    },
    "slack___post_message": {
        "inputSchema": {
            "type": "object",
            "properties": {
                key: {"type": "string"} for key in ("channel", "text", "run_marker")
            },
            "required": ["channel", "text", "run_marker"],
            "additionalProperties": False,
        },
        "scope": "chat:write",
    },
}
CLIENTS = ("cli", "claude-code", "codex")
CHECKERS = {"github": github_ac04, "jira": jira_ac04, "slack": slack_ac04}
ISSUES = {"github": 8075, "jira": 8076, "slack": 8077}
ATTRIBUTES = (
    "isolated",
    "authorized",
    "read_only",
    "identity_qualified",
    "cleanup_authorized",
    "deployed_target",
    "client_name",
    "version",
)
MAX_EVENTS = 256
MAX_BYTES = 4 * 1024 * 1024
# These are original criterion IDs, not replacement definitions for those epics.
# Their existing readers must qualify them before the final rollup can pass.
PREDECESSORS = {
    "8063": tuple(
        f"{issue}.AC-{number:02}"
        for issue in range(8064, 8081)
        for number in range(1, 5)
    )
    + tuple(f"8063.EC-{number:02}" for number in range(1, 4)),
    "8076": tuple(f"8076.AC-{number:02}" for number in range(1, 5)),
    "7266": tuple(f"TLS-S15-AC{number:02}" for number in range(1, 4)),
}


def digest(value):
    return hashlib.sha256(flow.canonical(value)).hexdigest()


def _bounded(value):
    if len(flow.canonical(value)) > MAX_BYTES:
        raise ValueError("Legacy evidence exceeds its private archive bound")
    return deepcopy(value)


def _target(target):
    if (
        type(target) is not dict
        or set(target) != {"gateway", "gateway_image", "facade_image"}
        or type(target["gateway"]) is not str
        or not re.fullmatch(r"[0-9a-f]{40}", target["gateway"])
        or any(
            type(target[key]) is not str
            or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", target[key])
            for key in ("gateway_image", "facade_image")
        )
    ):
        raise ValueError("Workflow-selected legacy deployment revisions required")


def _baseline():
    if digest(BASELINE) != BASELINE_SHA256:
        raise ValueError("The accepted legacy baseline changed")
    return {
        "revision": BASELINE_REVISION,
        "file_sha256": BASELINE_FILE_SHA256,
        "inventory_sha256": BASELINE_SHA256,
        "tools": deepcopy(BASELINE),
    }


def _catalogue(value, connector, target):
    expected = {
        key: val for key, val in BASELINE.items() if key.startswith(connector + "___")
    }
    if (
        type(value) is not dict
        or set(value) != {"source", "target", "tools"}
        or value["source"] != "independent-deployed-catalogue"
        or value["target"] != target
        or value["tools"] != expected
    ):
        raise ValueError(
            "Legacy names, input schemas or grant scopes differ from the pinned baseline"
        )


class _Recorder:
    def __init__(self, delegate, side, events):
        self.delegate, self.side, self.events = delegate, side, events

    def __getattr__(self, name):
        value = getattr(self.delegate, name)
        if not callable(value):
            if name not in ATTRIBUTES:
                raise ValueError("Unsupported legacy checker attribute")
            return value

        def call(*args, **kwargs):
            if len(self.events) >= MAX_EVENTS:
                raise ValueError("Legacy observation count exceeds its bound")
            event = {
                "side": self.side,
                "method": name,
                "args": _bounded(list(args)),
                "kwargs": _bounded(kwargs),
                "result": None,
                "error": None,
            }
            self.events.append(event)
            try:
                result = value(*args, **kwargs)
                event["result"] = _bounded(result)
                _bounded(self.events)
                return result
            except (
                FixtureUnavailable,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                TimeoutError,
                OSError,
            ) as exc:
                # Never retain exception text, which may include credentials.
                event["error"] = type(exc).__name__
                raise

        return call


class _Replay:
    def __init__(self, side, attributes, cursor):
        self.side, self.attributes, self.cursor = side, attributes, cursor

    def __getattr__(self, name):
        if name in ATTRIBUTES:
            if name not in self.attributes:
                raise AttributeError(name)
            return self.attributes[name]

        def call(*args, **kwargs):
            cursor = self.cursor
            if cursor["index"] >= len(cursor["events"]):
                raise ValueError("Missing independent legacy observation")
            event = cursor["events"][cursor["index"]]
            cursor["index"] += 1
            original = event["kwargs"].get("attempt_id")
            generated = kwargs.get("attempt_id")
            if original is not None:
                if str(UUID(original)) != original or generated is None:
                    raise ValueError("Invalid legacy attempt binding")
                if cursor["attempt"] is None:
                    cursor["attempt"] = (original, generated)
                if cursor["attempt"] != (original, generated):
                    raise ValueError("Observations cross legacy attempts")
                kwargs = {**kwargs, "attempt_id": original}
            if (
                event["side"] != self.side
                or event["method"] != name
                or flow.canonical(event["args"]) != flow.canonical(list(args))
                or flow.canonical(event["kwargs"]) != flow.canonical(kwargs)
                or event["error"] is not None
            ):
                raise ValueError("Legacy checker observations changed or failed")
            return deepcopy(event["result"])

        return call


def _attributes(driver):
    return {
        key: _bounded(getattr(driver, key))
        for key in ATTRIBUTES
        if hasattr(driver, key)
    }


def run_legacy_cell(connector, client, *, driver, observer, target, live):
    """Invoke the existing checker, including its original owned cleanup path."""
    _target(target)
    if connector not in CHECKERS or client not in CLIENTS or type(live) is not bool:
        raise ValueError("Unknown legacy connector or client")
    row = {
        "cell_id": f"{client}:{connector}",
        "scenario_id": f"SCN-{ISSUES[connector]}-04",
        "status": "blocked",
        "live": live,
        "catalogue": None,
        "attributes": {},
        "events": [],
        "result": None,
        "reason": None,
    }
    if (
        driver is None
        or observer is None
        or live
        and (
            getattr(driver, "deployed_target", False) is not True
            or getattr(observer, "deployed_target", False) is not True
        )
    ):
        row["reason"] = "missing-provider-access"
        return row
    try:
        # Establish schema/grant parity before any provider write can run.
        row["catalogue"] = _bounded(observer.catalogue(client_name=client))
        _catalogue(row["catalogue"], connector, target)
        row["attributes"] = _attributes(driver)
        result = CHECKERS[connector](
            _Recorder(driver, "driver", row["events"]),
            _Recorder(observer, "observer", row["events"]),
            client,
            target,
        )
        row["result"] = _bounded(result)
        row["status"] = "pass" if result.get("status") == "pass" else "blocked"
        if row["status"] != "pass":
            row["reason"] = "legacy-check-incomplete"
    except (
        FixtureUnavailable,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        TimeoutError,
        OSError,
    ):
        row["reason"] = "legacy-observation-unavailable"
    return _bounded(row)


def qualify_legacy_cell(row, *, connector, client, target, require_live=True):
    """Reapply original semantic assertions to all retained independent observations."""
    _target(target)
    if (
        connector not in CHECKERS
        or client not in CLIENTS
        or type(row) is not dict
        or set(row)
        != {
            "cell_id",
            "scenario_id",
            "status",
            "live",
            "catalogue",
            "attributes",
            "events",
            "result",
            "reason",
        }
        or row["cell_id"] != f"{client}:{connector}"
        or row["scenario_id"] != f"SCN-{ISSUES[connector]}-04"
        or row["status"] != "pass"
        or row["reason"] is not None
        or type(row["live"]) is not bool
        or require_live
        and row["live"] is not True
        or type(row["events"]) is not list
        or not 1 <= len(row["events"]) <= MAX_EVENTS
        or type(row["attributes"]) is not dict
        or not set(row["attributes"]) <= set(ATTRIBUTES)
        or require_live
        and row["attributes"].get("deployed_target") is not True
    ):
        raise ValueError("Complete executed legacy observations required")
    _bounded(row)
    _catalogue(row["catalogue"], connector, target)
    for event in row["events"]:
        if (
            type(event) is not dict
            or set(event) != {"side", "method", "args", "kwargs", "result", "error"}
            or event["side"] not in {"driver", "observer"}
            or type(event["method"]) is not str
            or type(event["args"]) is not list
            or type(event["kwargs"]) is not dict
            or event["error"] is not None
        ):
            raise ValueError("Malformed or unsuccessful legacy observations")
    cursor = {"events": row["events"], "index": 0, "attempt": None}
    expected = CHECKERS[connector](
        _Replay("driver", row["attributes"], cursor),
        _Replay("observer", {}, cursor),
        client,
        target,
    )
    if cursor["attempt"] is None:
        raise ValueError("Legacy operation attempt is missing")
    expected["attempt_id"] = cursor["attempt"][0]
    if (
        cursor["index"] != len(row["events"])
        or expected.get("status") != "pass"
        or expected.get("cleanup") != {"status": "confirmed"}
        or flow.canonical(expected) != flow.canonical(row["result"])
    ):
        raise ValueError("Legacy evidence does not satisfy the original checker")
    return {
        "cell_id": row["cell_id"],
        "scenario_id": row["scenario_id"],
        "status": "pass",
        "cleanup": "confirmed",
        "observations_sha256": digest(row),
    }


def collect_regression(*, binding, target, fixtures=None, live=True):
    """Run all nine legacy client cells; unavailable fixtures retain their rows.

    fixtures[(connector, client)] supplies {driver, observer}. The protected
    workflow owns their actual implementations; test fixtures are never loaded
    from repository paths or report-supplied module names here.
    """
    flow.validate_binding(binding)
    _target(target)
    from tests.e2e.tools.atlassian_regression_binding import LegacyCampaign

    if isinstance(fixtures, LegacyCampaign):
        from tests.e2e.tools.legacy_live_bindings import qualify_legacy

        if not live or fixtures.context["binding"] != binding or fixtures.context["target"] != target:
            raise ValueError("Legacy campaign differs from the selected run or target")
        observed = fixtures.collect()
        qualified = qualify_legacy(observed, manifest=fixtures.manifest)
        result = collect_regression(binding=binding, target=target, live=live)
        result["native_observations"] = observed
        result["native_parity"] = qualified
        by_cell = {row["cell"]: row for row in qualified["cases"]}
        for row in result["cells"]:
            client, connector = row["cell_id"].split(":")
            actual = by_cell[f"{connector}:{client}"]
            if actual["observable_parity"] == "pass":
                row["reason"] = "original-refresh-and-provider-request-counts-unverified"
        return result
    if type(live) is not bool or fixtures is not None and type(fixtures) is not dict:
        raise ValueError(
            "Explicit legacy execution mode and protected fixtures required"
        )
    fixtures = fixtures or {}
    if not set(fixtures) <= {
        (connector, client) for connector in CHECKERS for client in CLIENTS
    }:
        raise ValueError("Unknown legacy fixture selection")
    rows = []
    for connector in CHECKERS:
        for client in CLIENTS:
            fixture = fixtures.get((connector, client), {})
            if type(fixture) is not dict or not set(fixture) <= {"driver", "observer"}:
                raise ValueError("Invalid protected legacy fixture")
            rows.append(
                run_legacy_cell(
                    connector,
                    client,
                    driver=fixture.get("driver"),
                    observer=fixture.get("observer"),
                    target=target,
                    live=live,
                )
            )
    return {
        "schema": "atlassian-regression/v1",
        "binding": deepcopy(binding),
        "target": deepcopy(target),
        "baseline": _baseline(),
        "contract_sha256": digest(flow.completion_contract()),
        "cells": rows,
        "predecessor_inventory": {
            issue: list(criteria) for issue, criteria in PREDECESSORS.items()
        },
    }


def inspect_native_regression(evidence, *, manifest):
    from tests.e2e.tools.legacy_live_bindings import qualify_legacy

    if evidence.get("binding") != manifest["binding"] or evidence.get("target") != manifest["target"]:
        raise ValueError("Legacy observations differ from the protected release")
    result = qualify_legacy(evidence["native_observations"], manifest=manifest)
    if evidence.get("native_parity") != result:
        raise ValueError("Legacy parity differs from independent readback")
    return result


def qualify_regression(evidence, *, binding, target, require_live=True):
    """Validate every retained cell without trusting producer pass flags."""
    flow.validate_binding(binding)
    _target(target)
    if (
        type(evidence) is not dict
        or set(evidence)
        != {
            "schema",
            "binding",
            "target",
            "baseline",
            "contract_sha256",
            "cells",
            "predecessor_inventory",
        }
        or evidence["schema"] != "atlassian-regression/v1"
        or evidence["binding"] != binding
        or evidence["target"] != target
        or evidence["baseline"] != _baseline()
        or evidence["contract_sha256"] != digest(flow.completion_contract())
        or evidence["predecessor_inventory"]
        != {issue: list(criteria) for issue, criteria in PREDECESSORS.items()}
        or type(evidence["cells"]) is not list
        or len(evidence["cells"]) != 9
    ):
        raise ValueError(
            "Original legacy inventory and selected release binding required"
        )
    _bounded(evidence)
    qualified = []
    for row, (connector, client) in zip(
        evidence["cells"],
        ((connector, client) for connector in CHECKERS for client in CLIENTS),
        strict=True,
    ):
        qualified.append(
            qualify_legacy_cell(
                row,
                connector=connector,
                client=client,
                target=target,
                require_live=require_live,
            )
        )
    return {
        "case_id": "legacy-all-verbs",
        "status": "pass",
        "cells": qualified,
        "baseline_sha256": BASELINE_SHA256,
        "evidence_sha256": digest(evidence),
    }


def regression_rollup(
    live_report, stage_reports, evaluation, *, binding, qualified_scenarios
):
    """Apply every original predicate; an unqualified predecessor stays incomplete."""
    return flow.assemble_evaluation_summary(
        live_report,
        stage_reports,
        evaluation,
        binding=binding,
        qualified_scenarios=qualified_scenarios,
    )


def predecessor_gaps(evidence, *, binding, target, require_live=True):
    """Report exactly which original ACs the executed connector matrix qualifies.

    AC-04 is the original per-client connector scenario. It does not implicitly
    qualify that story's other ACs, the access-repair epic, or the older Slack
    scope-denial/documentation criteria. Those need their original readers.
    """
    # Validate the immutable inventory even when some cells cannot qualify.
    expected = collect_regression(binding=binding, target=target, live=False)
    if (
        type(evidence) is not dict
        or set(evidence) - {"native_observations", "native_parity"} != set(expected)
        or any(evidence[key] != expected[key] for key in expected if key != "cells")
        or type(evidence["cells"]) is not list
        or len(evidence["cells"]) != 9
    ):
        raise ValueError(
            "Bound legacy evidence and complete predecessor inventory required"
        )
    qualified = set()
    for connector in CHECKERS:
        for client in CLIENTS:
            rows = [
                row
                for row in evidence["cells"]
                if type(row) is dict and row.get("cell_id") == f"{client}:{connector}"
            ]
            if len(rows) != 1:
                raise ValueError("Missing or duplicate legacy cell")
            try:
                qualify_legacy_cell(
                    rows[0],
                    connector=connector,
                    client=client,
                    target=target,
                    require_live=require_live,
                )
            except (
                ValueError,
                FixtureUnavailable,
                KeyError,
                TypeError,
                AttributeError,
            ):
                break
        else:
            qualified.add(f"{ISSUES[connector]}.AC-04")
    return {
        issue: [
            {
                "criterion_id": criterion,
                "status": "pass" if criterion in qualified else "unverified",
                "evidence_verified": criterion in qualified,
            }
            for criterion in criteria
        ]
        for issue, criteria in PREDECESSORS.items()
    }
