"""Terraform-owned, conditional registration of one exact protected domain pair.

Only this fixed producer/worker recipe is accepted. This is not a general registry
writer. It never updates, restores, deletes or expands an existing registration.
"""

import hashlib
import json
import os
import re
import sys
import uuid


class Refused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def registry_id(role_arn):
    # Role ARN survives IAM recreation and changes of organization/operator.
    # Every writer therefore conditions on the SAME primary key for this role;
    # the eventually consistent role index is only an extra collision check.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "adp:domain-operation-registration:v1:" + role_arn))


def role(arn, account):
    require(
        isinstance(arn, str)
        and re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_-]+", arn),
        "exact same-account role required",
    )
    return arn.rsplit("/", 1)[1]


def principal(arn, account):
    if arn.startswith(f"arn:aws:sts::{account}:assumed-role/"):
        bits = arn.split("/")
        require(len(bits) == 3, "path-qualified operator role is unsupported")
        return f"arn:aws:iam::{account}:role/{bits[1]}"
    role(arn, account)
    return arn


def records(document):
    require(
        isinstance(document, dict)
        and set(document)
        == {
            "version",
            "account_id",
            "region",
            "environment",
            "operator_role_arn",
            "registry_table",
            "domain_org_id",
            "adp_org_id",
            "producer",
            "worker",
        },
        "closed domain registration document required",
    )
    require(
        type(document["version"]) is int and document["version"] == 1,
        "unsupported registration version",
    )
    account, environment = document["account_id"], document["environment"]
    require(re.fullmatch(r"[0-9]{12}", account or ""), "invalid account")
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,19}", environment or ""), "invalid environment")
    require(re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", document["region"] or ""), "invalid region")
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", document["registry_table"] or ""),
        "invalid registry table",
    )
    require(
        str(uuid.UUID(document["domain_org_id"])) == document["domain_org_id"],
        "canonical domain organization UUID required",
    )
    require(
        isinstance(document["adp_org_id"], str) and 1 <= len(document["adp_org_id"]) <= 255,
        "invalid ADP organization",
    )
    role(document["operator_role_arn"], account)
    result = []
    for kind, scopes, expected_name in (
        ("producer", ["domain:operation-producer"], f"adp-{environment}-superplane-api-producer"),
        (
            "worker",
            ["domain:operation-executor", "domain:operation-recovery"],
            f"adp-{environment}-superplane-domain-worker",
        ),
    ):
        entry = document[kind]
        require(
            isinstance(entry, dict) and set(entry) == {"agent_id", "role_arn", "role_id"},
            "closed role binding required",
        )
        require(
            str(uuid.UUID(entry["agent_id"])) == entry["agent_id"],
            "canonical registry UUID required",
        )
        require(
            role(entry["role_arn"], account) == expected_name,
            "role is outside maintained domain runtime recipe",
        )
        require(
            entry["agent_id"] == registry_id(entry["role_arn"]),
            "registry UUID must be derived from the exact role ARN",
        )
        require(
            re.fullmatch(r"AROA[A-Z0-9]{16,32}", entry["role_id"] or ""),
            "immutable IAM role identity required",
        )
        identity = {
            "domain": "superplane",
            "domain_org_id": document["domain_org_id"],
            "adp_org_id": document["adp_org_id"],
            "kind": kind,
            **entry,
        }
        item = {
            "agent_id": {"S": entry["agent_id"]},
            "role_arn": {"S": entry["role_arn"]},
            "agent_name": {"S": f"superplane-{kind}"},
            "org_id": {"S": document["adp_org_id"]},
            "scope": {"S": "internal"},
            "status": {"S": "active"},
            "owner": {"S": "webhook-terraform-domain-operations-v1"},
            "credential_scopes": {"SS": scopes},
            "registration_revision": {
                "S": hashlib.sha256(canonical(identity).encode()).hexdigest()
            },
            "iam_role_id": {"S": entry["role_id"]},
            "domain_org_id": {"S": document["domain_org_id"]},
            "registered_by": {"S": document["operator_role_arn"]},
        }
        # Producer signs its own narrow service requests. Worker execution still
        # independently requires pod proof plus a live delegated run credential.
        if kind == "worker":
            item["requires_run_identity"] = {"BOOL": True}
        result.append(item)
    require(result[0]["agent_id"] != result[1]["agent_id"], "distinct registry IDs required")
    return result


def same_item(left, right):
    def normalize(value):
        return {key: sorted(val) if key == "SS" else val for key, val in value.items()}

    return left is not None and {k: normalize(v) for k, v in left.items()} == {
        k: normalize(v) for k, v in right.items()
    }


def register(document, session, *, check_only=False):
    desired = records(document)
    sts = session.client("sts", region_name=document["region"])
    caller = sts.get_caller_identity()
    require(
        caller["Account"] == document["account_id"]
        and principal(caller["Arn"], caller["Account"]) == document["operator_role_arn"],
        "selected operator identity changed",
    )
    iam = session.client("iam", region_name=document["region"])
    for kind in ("producer", "worker"):
        expected = document[kind]
        observed = iam.get_role(RoleName=role(expected["role_arn"], document["account_id"]))["Role"]
        require(
            observed["Arn"] == expected["role_arn"] and observed["RoleId"] == expected["role_id"],
            "registered role was replaced or changed",
        )
    ddb = session.client("dynamodb", region_name=document["region"])
    table = document["registry_table"]
    existing = []
    for item in desired:
        key = {"agent_id": item["agent_id"]}
        old = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
        require(
            old is None or same_item(old, item),
            "registry identity exists with different owner, status or authority",
        )
        # The maintained IAM authentication resolves this index. Refuse ambiguous
        # old role mappings; never overwrite or delete their owners.
        response = ddb.query(
            TableName=table,
            IndexName="by-role-arn",
            KeyConditionExpression="role_arn = :role",
            ExpressionAttributeValues={":role": item["role_arn"]},
            Limit=2,
        )
        require(
            not response.get("LastEvaluatedKey")
            and all(same_item(row, item) for row in response.get("Items", []))
            and len(response.get("Items", [])) <= 1,
            "role already has another registry mapping",
        )
        existing.append(old)
    require(
        all(existing) or not any(existing),
        "partial or conflicting registration pair requires owner reconciliation",
    )
    if not any(existing) and not check_only:
        # One atomic conditional write. A lost reply is reconciled by the same
        # consistent reads on retry, not by unconditional PutItem or a new ID.
        ddb.transact_write_items(
            ClientRequestToken=hashlib.sha256(canonical(desired).encode()).hexdigest()[:36],
            TransactItems=[
                {
                    "Put": {
                        "TableName": table,
                        "Item": item,
                        "ConditionExpression": "attribute_not_exists(agent_id)",
                    }
                }
                for item in desired
            ],
        )
        for item in desired:
            observed = ddb.get_item(
                TableName=table, Key={"agent_id": item["agent_id"]}, ConsistentRead=True
            ).get("Item")
            require(
                same_item(observed, item), "registry write needs original-identity reconciliation"
            )
    return {
        "version": 1,
        "state": "verified" if all(existing) or not check_only else "absent",
        "registration_revisions": [item["registration_revision"]["S"] for item in desired],
    }


def main():
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    # Explicit finite network retries, never printing SDK exception bodies.
    class Session:
        def client(self, name, **kwargs):
            return boto3.client(
                name,
                **kwargs,
                config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1}),
            )

    try:
        require(sys.argv[1:] in ([], ["--check"]), "unsupported registration arguments")
        document = json.loads(os.environ["ADP_DOMAIN_REGISTRATION_DOCUMENT"])
        result = register(document, Session(), check_only=sys.argv[1:] == ["--check"])
        print(canonical(result))
    except (BotoCoreError, ClientError, ValueError, TypeError, KeyError, OSError):
        raise SystemExit(
            "domain registration refused or outcome unknown; reconcile the same identities"
        ) from None


if __name__ == "__main__":
    main()
