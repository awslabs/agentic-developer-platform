"""Account for remote decisions, review actions and every harness intervention."""

from tests.e2e.orchestration.report import Intervention
from .http import Unsupported


def collect(session, pull_requests):
    entries = list(session.interventions)
    decisions, histories, provider_events = [], [], []
    for resource in session.inventory.fixtures:
        if resource.kind != "qualification-flow" or not resource.observed_resource_id:
            continue
        path = f"/orchestration/flows/{resource.observed_resource_id}"
        graph = session.client.get(path)
        rows = session.client.get(path + "/decisions")
        decisions.extend(rows)
        refs = {n["id"]: n["node_ref"] for n in graph["nodes"]}
        for node in graph["nodes"]:
            if node["kind"] not in {"story", "eval"}:
                continue
            history = node.get("execution_history") or {}
            if not history.get("history_complete"):
                raise Unsupported("authenticated invocation history is incomplete")
            histories.append(history)
        for row in rows:
            kind = None
            target = refs.get(row["node_id"], "plan")
            if row["kind"] in {"node_resumed", "halt_overridden", "replan_requested"}:
                kind = "coordinator_retrigger"
            elif row["actor_kind"] == "human":
                if row["kind"] == "plan_accepted":
                    kind = "planned_setup"
                elif row["kind"] in {"gate_approved", "gate_rejected"} and target in {
                    "release",
                    "refuse",
                }:
                    kind, target = (
                        "planned_gate",
                        "refusal" if target == "refuse" else target,
                    )
                else:
                    kind = "unplanned"
            if kind:
                artifact = session.evidence.save(
                    "decision-intervention", row, "gateway:append-only-decisions"
                )
                entries.append(
                    Intervention(
                        at=row["created_at"],
                        actor=row["actor_id"],
                        kind=kind,
                        target=target,
                        evidence=artifact,
                    )
                )
    for row in pull_requests:
        pr = row["pr"]
        path = f"/repos/{session.config.repository}/pulls/{pr['number']}"
        # Preserve the complete paginated provider history, including comments
        # that could request an off-plan restart. No worker declaration supplies
        # this accounting and a pagination overflow refuses completeness.
        events = {
            "pr": pr,
            "reviews": row["reviews"],
            "commits": session.client.pages(path + "/commits"),
            "comments": session.client.pages(path + "/comments"),
            "timeline": session.client.pages(
                f"/repos/{session.config.repository}/issues/{pr['number']}/timeline"
            ),
        }
        provider_events.append(events)
        artifact = session.evidence.save(
            "review-interventions", events, "github:reviews/commits/timeline"
        )
        for review in row["reviews"]:
            if review.get("submitted_at"):
                entries.append(
                    Intervention(
                        at=review["submitted_at"],
                        actor="github:" + str(review["user"]["id"]),
                        kind="required_review",
                        target=str(pr["number"]),
                        evidence=artifact,
                    )
                )
        # A manual provider retry is a coordinator intervention even when it
        # produces a successful result. Preserve the original command text.
        for event in events["timeline"]:
            body = (event.get("body") or "").lower()
            if event.get("event") == "commented" and any(
                command in body
                for command in ("/retry", "/resume", "@adp-bot run", "@adp-bot restart")
            ):
                entries.append(
                    Intervention(
                        at=event["created_at"],
                        actor="github:" + str(event["actor"]["id"]),
                        kind="coordinator_retrigger",
                        target=str(pr["number"]),
                        evidence=artifact,
                    )
                )
    if len(pull_requests) != 2 or not decisions or not histories:
        raise Unsupported("delivery intervention audit is incomplete")
    # Journal files exist before each native effect. Every one must have an
    # attributed entry even when the process died before returning its receipt.
    journal_names = {
        p.stem.removeprefix("fault-")
        for p in session.inventory.path.parent.glob("fault-*.json")
    }
    if not journal_names <= {i.target for i in entries if i.kind == "fault"}:
        raise Unsupported("unaccounted fault injection journal")
    session.interventions = entries
    return {
        "entries": [i.model_dump(mode="json") for i in entries],
        "decisions": decisions,
        "histories": histories,
        "provider_events": provider_events,
        "scope": "authenticated flow decisions, complete invocation lineage, provider timeline and harness journals",
        # The live control API exposes a bounded worker acknowledgement journal,
        # not a durable complete command history. Absence from a final snapshot
        # cannot prove there was no pause/resume/steer between polls.
        "complete": False,
        "remaining_boundary": "https://github.com/aws-e/adp/issues/4539: durable per-run control intervention history",
    }
