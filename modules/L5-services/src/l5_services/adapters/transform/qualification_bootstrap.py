"""Bounded Product submission with a durable identity across driver restarts.

The Product API owns admission, current authority, budget and native execution.
This client never calls a provider, creates a new retry, or changes configuration.
"""

import asyncio
import fcntl
import json
import os
import re
import time
from hashlib import sha256
from urllib.parse import quote

from l5_services.run_input import TransformRunInput


SCOPE_KEYS = {"tenant_id", "product_id", "project_id"}
PENDING = {"admitted", "queued", "capturing", "preparing", "running", "submitted",
           "collecting", "publishing", "waiting", "baseline", "verifying"}


class BootstrapIncomplete(ValueError):
    """An actionable, non-secret diagnostic safe for the release log."""

    def __init__(self, code, action):
        self.code = code
        self.action = action
        super().__init__(f"{code}: {action}")


def validate_bootstrap(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2:
        raise ValueError("Select one or two explicit Transform qualification inquiries")
    keys = set()
    for selected in entries:
        if (not isinstance(selected, dict)
                or set(selected) != {"scope", "input", "idempotency_key", "max_wait_seconds"}
                or not isinstance(selected["scope"], dict) or set(selected["scope"]) != SCOPE_KEYS
                or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value)
                       for value in selected["scope"].values())
                or not isinstance(selected["idempotency_key"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", selected["idempotency_key"])
                or type(selected["max_wait_seconds"]) is not int
                or not 1 <= selected["max_wait_seconds"] <= 1800):
            raise ValueError("Transform bootstrap requires exact scope, request, durable key and time bound")
        request = TransformRunInput.model_validate(selected["input"])
        if any((source.tenant_id, source.project_id) != (
                selected["scope"]["tenant_id"], selected["scope"]["project_id"])
               for source in [*request.sources, *([request.parent_task_ref] if request.parent_task_ref else [])]):
            raise ValueError("Transform bootstrap source differs from selected scope")
        if selected["idempotency_key"] in keys:
            raise ValueError("Each qualification inquiry needs a distinct durable request key")
        keys.add(selected["idempotency_key"])


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _scoped(value, scope):
    return isinstance(value, dict) and all(value.get(key) == expected for key, expected in scope.items())


def _identity(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value) is not None


def _run_matches(run, selected, run_id=None):
    return (isinstance(run, dict) and _identity(run.get("run_id"))
            and (run_id is None or run["run_id"] == run_id)
            and _scoped(run.get("scope"), selected["scope"])
            and run.get("input") == selected["input"])


async def prepare(fixture, client, directory, *, allow_submit=True, clock=time.time, sleep=asyncio.sleep,
                  on_selected=None):
    """Start each selected request once; resume only its acknowledged run.

    Lost submission acknowledgments replay the same Product idempotency key and
    body. A journal lock prevents competing local drivers. The first wall-clock
    deadline is retained, so restarting cannot replenish the polling allowance.
    """
    validate_bootstrap(fixture["bootstrap"])
    response = await client.get("/auth/me", follow_redirects=False)
    response.raise_for_status()
    actor = response.json()
    if (not isinstance(actor, dict) or not _identity(actor.get("user_id"))
            or not _identity(actor.get("org_id")) or actor.get("account_type") != "human"):
        raise ValueError("Transform qualification requires an authenticated dedicated human fixture identity")
    selections = []
    for selected in fixture["bootstrap"]:
        if actor["org_id"] != selected["scope"]["tenant_id"]:
            raise ValueError("Transform qualification identity differs from the selected tenant")
        root = (f"/v1/products/{quote(selected['scope']['product_id'], safe='')}/projects/"
                f"{quote(selected['scope']['project_id'], safe='')}/services/transform")
        request_digest = _digest({"source_sha": fixture["source_sha"], "selection": selected,
                                  "actor_id": actor["user_id"], "tenant_id": actor["org_id"]})
        path = directory / ("transform-bootstrap-" + _digest(selected["idempotency_key"]) + ".jsonl")
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as journal:
            if os.fstat(journal.fileno()).st_mode & 0o077:
                raise ValueError("Transform bootstrap journal must remain private")
            try:
                fcntl.flock(journal.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BootstrapIncomplete("bootstrap_in_progress", "Wait for the existing driver; retain its request key.") from None
            journal.seek(0)
            raw = journal.read(262145)
            if len(raw) > 262144:
                raise ValueError("Transform bootstrap journal exceeds its bound")
            records = [json.loads(line) for line in raw.splitlines()]
            if any(not isinstance(row, dict) or row.get("request_sha256") != request_digest for row in records):
                raise BootstrapIncomplete("bootstrap_selection_changed", "Restore the original source revision and request; do not replace its key.")

            def record(state, **values):
                row = {"request_sha256": request_digest, "state": state, **values}
                journal.write(json.dumps(row, sort_keys=True) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                records.append(row)

            if not records:
                if not allow_submit:
                    raise BootstrapIncomplete("bootstrap_not_run", "Execute the bounded qualification run phase before emitting evidence.")
                # Readiness checks have no provider effects and precede the durable intent.
                response = await client.get(root + "/configuration", follow_redirects=False)
                response.raise_for_status()
                configuration = response.json()
                response = await client.get(root + "/bindings/" + quote(selected["input"]["binding_id"], safe=""),
                                            follow_redirects=False)
                response.raise_for_status()
                binding = response.json()
                if (not _scoped(configuration.get("scope"), selected["scope"])
                        or configuration.get("revision") != selected["input"]["configuration_revision"]
                        or configuration.get("state") != "ready"
                        or (configuration.get("settings") or {}).get("enabled") is not True
                        or not _scoped(binding.get("scope"), selected["scope"])
                        or binding.get("binding_id") != selected["input"]["binding_id"]
                        or binding.get("configuration_id") != configuration.get("configuration_id")
                        or binding.get("state") != "ready"):
                    raise BootstrapIncomplete("bootstrap_binding_not_ready", "Qualify and link the selected Product configuration before native submission.")
                record("prepared", deadline=clock() + selected["max_wait_seconds"])
            deadline = records[0].get("deadline")
            if type(deadline) not in (int, float):
                raise ValueError("Transform bootstrap journal has no original deadline")
            acknowledged = [row for row in records if row["state"] == "acknowledged"]
            run_id = acknowledged[-1].get("run_id") if acknowledged else None
            if run_id is not None and not _identity(run_id):
                raise ValueError("Transform bootstrap journal contains an invalid run identity")
            if run_id is None:
                if not allow_submit:
                    raise BootstrapIncomplete("bootstrap_submit_outcome_unknown", "Reconcile the original request in the run phase; evidence emission cannot submit native work.")
                if clock() >= deadline:
                    raise BootstrapIncomplete("bootstrap_submit_outcome_unknown", "Reconcile the original Product request key; the submit acknowledgment was not retained.")
                # The server reauthorizes a replay and resolves its original receipt.
                response = await client.post(root + "/runs", json=selected["input"],
                    headers={"Idempotency-Key": selected["idempotency_key"]}, follow_redirects=False)
                response.raise_for_status()
                run = response.json()
                if response.status_code != 202 or not _run_matches(run, selected):
                    raise BootstrapIncomplete("bootstrap_acknowledgment_mismatch", "Reconcile the original Product request key; no matching run was acknowledged.")
                run_id = run["run_id"]
                record("acknowledged", run_id=run_id)
            while True:
                response = await client.get(root + "/runs/" + quote(run_id, safe=""), follow_redirects=False)
                response.raise_for_status()
                run = response.json()
                if not _run_matches(run, selected, run_id):
                    raise ValueError("Transform run readback differs from original request")
                if run.get("state") == "succeeded":
                    break
                if run.get("state") not in PENDING:
                    record("attention", run_id=run_id, observation_sha256=_digest(run))
                    raise BootstrapIncomplete("bootstrap_run_needs_attention", "Inspect the retained original run and resolve its reported blocker; this driver never creates a replacement run.")
                if clock() >= deadline:
                    raise BootstrapIncomplete("bootstrap_wait_exhausted", "Observe the original run; its saved polling allowance is exhausted and no new run was submitted.")
                await sleep(min(2, deadline - clock()))
            # Enumerate bounded scoped pages; never select the newest unrelated result.
            cursor = None
            seen = set()
            result = None
            for _ in range(10):
                response = await client.get(root + "/results", params={"limit": 100, **({"cursor": cursor} if cursor else {})},
                                            follow_redirects=False)
                response.raise_for_status()
                page = response.json()
                for item in page.get("items", []):
                    if item.get("run_id") == run_id:
                        if result is not None:
                            raise ValueError("Transform run has ambiguous results")
                        result = item
                cursor = page.get("next_cursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError("Transform result pagination repeated a cursor")
                seen.add(cursor)
            else:
                raise BootstrapIncomplete("bootstrap_result_page_limit", "Read the original run's result explicitly; bounded result enumeration was exhausted.")
            if (not isinstance(result, dict) or not _identity(result.get("result_id"))
                    or not _scoped(result.get("scope"), selected["scope"])
                    or type(result.get("revision")) is not int or result["revision"] < 1):
                raise BootstrapIncomplete("bootstrap_result_unavailable", "Repair result collection for the original run; no native resubmission is needed.")
            selection = {**selected["scope"], "run_id": run_id, "result_id": result["result_id"],
                         "result_revision": result["revision"]}
            if not any(row["state"] == "selected" and row.get("selection") == selection for row in records):
                record("selected", selection=selection)
            selections.append(selection)
            if on_selected is not None:
                await on_selected(selection, len(selections))
    return {"source_sha": fixture["source_sha"], "runs": selections}
