"""E25: served research reads only; no scan, proposal write or approval."""

import story_reads
import common


def research(cli, evidence):
    base = ["superplane", "research"]
    for command, field in [
        (["findings", "list", "--page-size", "1"], "items"),
        (["proposal", "list", "--page-size", "1"], "items"),
        (["sources"], "sources"),
        (["stats"], "total_findings"),
    ]:
        value = story_reads.detail(cli.json([*base, *command]))
        common.require(field in value, "Research read omitted its contract field")
        if field in {"items", "sources"}:
            common.require(
                isinstance(value[field], list), "Research collection is not a list"
            )
        if field == "items":
            common.require(
                type(value.get("complete")) is bool,
                "Research pagination completeness missing",
            )
            for row in value[field]:
                common.require(
                    isinstance(row, dict) and row.get("id"), "Research identity missing"
                )
    evidence.update(
        cases=["findings-page", "proposal-page", "sources", "stats"],
        qualification="Read-only regression; scan/replay/decision acceptance remains open",
    )


def execute(config, evidence):
    story_reads.SCENARIOS["research"] = research
    story_reads.execute({**config, "mode": "research"}, evidence)
