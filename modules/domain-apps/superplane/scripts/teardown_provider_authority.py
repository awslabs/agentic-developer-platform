"""App-owned provider authority decommissioning hooks for the webhook state owner."""

import json
import time
from pathlib import Path

PREFIX = "module.superplane_provider_authority[0]."
OWNED = {
    "terraform_data.owner",
    "aws_dynamodb_table.authority",
    "aws_dynamodb_table.evidence",
    "aws_dynamodb_resource_policy.authority",
    "aws_iam_policy.gateway",
    "aws_iam_role_policy_attachment.gateway",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def table_identity(run, call, attrs):
    name = attrs["name"]
    require(
        name
        in (
            f"adp-{run.environment}-superplane-provider-authorities",
            f"adp-{run.environment}-superplane-provider-evidence",
        ),
        "Unexpected provider table",
    )
    arn = f"arn:aws:dynamodb:{run.region}:{run.account}:table/{name}"
    require(attrs["arn"] == arn, "Provider table target differs")
    table = call(
        "dynamodb",
        "describe-table",
        "--table-name",
        name,
        absent=("ResourceNotFoundException",),
    )
    if table is None:
        return None
    table = table["Table"]
    tags = {
        t["Key"]: t["Value"]
        for t in call("dynamodb", "list-tags-of-resource", "--resource-arn", arn)[
            "Tags"
        ]
    }
    expected = {
        "ManagedBy": "superplane-provider-authority",
        "DomainApp": "superplane",
        "Environment": run.environment,
    }
    require(
        table["TableArn"] == arn
        and all(
            tags.get(k) == v and attrs.get("tags", {}).get(k) == v
            for k, v in expected.items()
        ),
        "Provider table ownership differs",
    )
    return {
        "name": name,
        "arn": arn,
        "table_id": table["TableId"],
        "created": table["CreationDateTime"],
    }


def prepare(run, state, shared):
    source = (
        Path(__file__).resolve().parents[1] / "infra/provider-authority/decommission"
    )
    override = {"module": {"superplane_provider_authority": {"source": str(source)}}}
    rows = [(a, k, v) for a, k, v in shared.instances(state) if a.startswith(PREFIX)]
    if not rows:
        # A retry can have removed this child while other webhook resources remain.
        # Keep its provisioning data sources disabled through the whole teardown.
        return override
    require(
        all(a.removeprefix(PREFIX) in OWNED for a, _, _ in rows),
        "Unrecognized provider authority resource",
    )
    tables = {}
    for address, kind, attrs in rows:
        if kind == "aws_dynamodb_table":
            tables[address] = table_identity(run, shared.aws, attrs)
        elif kind == "aws_iam_policy":
            require(
                attrs["arn"]
                == f"arn:aws:iam::{run.account}:policy/adp-{run.environment}-superplane-provider-authority",
                "Provider authority policy target differs",
            )
        elif kind == "aws_iam_role_policy_attachment":
            require(
                attrs["role"] == f"adp-{run.environment}-role-gateway-service"
                and attrs["policy_arn"]
                == f"arn:aws:iam::{run.account}:policy/adp-{run.environment}-superplane-provider-authority",
                "Provider authority attachment target differs",
            )
    path = run.directory / "provider-authority-tables.json"
    if path.exists():
        previous = json.loads(path.read_text())
        require(
            all(
                previous.get(a) == identity
                for a, identity in tables.items()
                if identity is not None
            ),
            "Provider authority tables changed since review",
        )
    else:
        shared.write_json(path, tables)
    return override


def before_apply(run, rows, shared):
    selected = [r for r in rows if r["address"].startswith(PREFIX)]
    if not selected:
        return
    require(
        all(
            r["address"].removeprefix(PREFIX) in OWNED
            and r["change"]["actions"] == ["delete"]
            for r in selected
        ),
        "Provider authority requires a delete-only plan",
    )
    reviewed = json.loads(
        (run.directory / "provider-authority-tables.json").read_text()
    )
    for row in selected:
        if row["type"] != "aws_dynamodb_table":
            continue
        current = table_identity(run, shared.aws, row["change"]["before"])
        if current is None:
            continue
        require(
            current == reviewed.get(row["address"]),
            "Provider table was replaced after review",
        )
        shared.aws(
            "dynamodb",
            "update-table",
            "--table-name",
            current["name"],
            "--no-deletion-protection-enabled",
        )
        deadline = time.monotonic() + 120
        while True:
            table = shared.aws(
                "dynamodb", "describe-table", "--table-name", current["name"]
            )["Table"]
            require(
                table["TableId"] == current["table_id"],
                "Provider table identity changed",
            )
            if table["TableStatus"] == "ACTIVE" and not table.get(
                "DeletionProtectionEnabled"
            ):
                break
            require(
                time.monotonic() < deadline, "Provider table protection did not settle"
            )
            time.sleep(3)
