"""Read explicit tools grants from the existing protected authority table."""

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from src.internal.tools_platform import implicitly_allowed


class ToolsPolicyStore:
    def __init__(self, *, table_name, client):
        self.table_name = table_name
        self.client = client

    def get(self, *, tenant_id, owner_id):
        try:
            item = self.client.get_item(
                TableName=self.table_name,
                Key={"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"TOOLS_POLICY#{owner_id}"}},
                ConsistentRead=True,
            ).get("Item")
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool policy unavailable") from None
        if item is None:
            return None
        try:
            policy = {key: TypeDeserializer().deserialize(value) for key, value in item.items()}
            if (
                policy.get("record_type") != "TOOLS_POLICY"
                or policy.get("schema_version") != "1.0"
                or policy.get("tenant_id") != tenant_id
                or policy.get("owner_id") != owner_id
            ):
                raise ValueError("Tools policy owner mismatch")
        except (TypeError, ValueError, KeyError):
            raise HTTPException(503, "tool policy unavailable") from None
        return policy

    def ensure_policy(self, *, tenant_id, owner_id):
        """Create the owner's empty active policy row when none exists (#8113).

        First-party read-only tools are implicitly allowed once the tenant entitles them,
        so an owner may consent before any admin has written their ``allowed_tools``.
        Consent needs a row to fence on; this writes the same shape ``put_allowed`` creates,
        with nothing allowed and nothing consented, at revision 0. A concurrent creator
        winning the race is not an error: the row exists either way.
        """
        serializer = TypeSerializer()
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "pk": {"S": f"TENANT#{tenant_id}"},
                    "sk": {"S": f"TOOLS_POLICY#{owner_id}"},
                    **{
                        name: serializer.serialize(value)
                        for name, value in {
                            "record_type": "TOOLS_POLICY",
                            "schema_version": "1.0",
                            "tenant_id": tenant_id,
                            "owner_id": owner_id,
                            "status": "active",
                            "allowed_tools": [],
                            "consented_tools": [],
                            "tool_max_cost_usd": {},
                            "catalog": [],
                            "revision": 0,
                        }.items()
                    },
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise HTTPException(503, "tool policy unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool policy unavailable") from None

    def put_consent(
        self,
        *,
        tenant_id,
        owner_id,
        allowed_tools,
        consented_tools,
        previous_consent,
        expected_revision,
        consent_mode="explicit",
        opted_out_tools=(),
    ):
        """Write the owner's consent decision under the row's revision fence.

        ``consent_mode`` ``explicit`` stores the opt-in list (pre-#8387 behaviour); ``default``
        stores an empty list plus ``opted_out_tools`` and lets the tenant entitlement be the
        consent (``tools_effective_policy``). Both attributes are written on every call so a
        legacy row gains them the first time its owner decides.
        """
        if consent_mode not in ("default", "explicit"):
            raise HTTPException(503, "tool policy unavailable")
        revision_condition = "(attribute_not_exists(revision) OR revision = :expected)" if expected_revision == 0 else "revision = :expected"
        consent_condition = (
            "(attribute_not_exists(consented_tools) OR consented_tools = :previous)" if not previous_consent else "consented_tools = :previous"
        )
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"TOOLS_POLICY#{owner_id}"}},
                UpdateExpression="SET consented_tools = :consent, consent_mode = :mode, opted_out_tools = :opted, revision = :next",
                ConditionExpression=(
                    "record_type = :record AND schema_version = :version AND "
                    "tenant_id = :tenant AND owner_id = :owner AND #status = :active AND allowed_tools = :allowed "
                    f"AND {revision_condition} AND {consent_condition}"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":record": {"S": "TOOLS_POLICY"},
                    ":version": {"S": "1.0"},
                    ":tenant": {"S": tenant_id},
                    ":owner": {"S": owner_id},
                    ":active": {"S": "active"},
                    ":allowed": {"L": [{"S": tool} for tool in allowed_tools]},
                    ":consent": {"L": [{"S": tool} for tool in consented_tools]},
                    ":mode": {"S": consent_mode},
                    ":opted": {"L": [{"S": tool} for tool in opted_out_tools]},
                    ":previous": {"L": [{"S": tool} for tool in previous_consent]},
                    ":expected": {"N": str(expected_revision)},
                    ":next": {"N": str(expected_revision + 1)},
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise HTTPException(409, "tool policy changed; reload and retry") from None
            raise HTTPException(503, "tool policy unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool policy unavailable") from None

    def put_allowed(self, *, tenant_id, owner_id, allowed_tools, tool_max_cost_usd, catalog, expected_revision):
        current = self.get(tenant_id=tenant_id, owner_id=owner_id)
        revision = current.get("revision", 0) if current else 0
        if revision != expected_revision or (current and current.get("status") != "active"):
            raise HTTPException(409, "tool policy changed; reload and retry")
        consented = current.get("consented_tools", []) if current else []
        if not isinstance(consented, list) or not all(isinstance(tool, str) for tool in consented):
            raise HTTPException(503, "tool policy unavailable")
        # Consent to a first-party read-only tool rides on the tenant entitlement, not on this
        # per-user allow list, so an admin write never silently revokes it (#8113).
        consented = [tool for tool in consented if tool in allowed_tools or implicitly_allowed(tool)]
        serializer = TypeSerializer()
        key = {"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"TOOLS_POLICY#{owner_id}"}}
        values = {
            ":allowed": serializer.serialize(allowed_tools),
            ":consent": serializer.serialize(consented),
            ":ceilings": serializer.serialize(tool_max_cost_usd),
            ":catalog": serializer.serialize(catalog),
            ":next": {"N": str(revision + 1)},
        }
        try:
            if current is None:
                self.client.put_item(
                    TableName=self.table_name,
                    Item={
                        **key,
                        **{
                            name: serializer.serialize(value)
                            for name, value in {
                                "record_type": "TOOLS_POLICY",
                                "schema_version": "1.0",
                                "tenant_id": tenant_id,
                                "owner_id": owner_id,
                                "status": "active",
                                "allowed_tools": allowed_tools,
                                "consented_tools": [],
                                "tool_max_cost_usd": tool_max_cost_usd,
                                "catalog": catalog,
                                "revision": 1,
                            }.items()
                        },
                    },
                    ConditionExpression="attribute_not_exists(pk)",
                )
            else:
                values.update(
                    {
                        ":record": {"S": "TOOLS_POLICY"},
                        ":version": {"S": "1.0"},
                        ":tenant": {"S": tenant_id},
                        ":owner": {"S": owner_id},
                        ":active": {"S": "active"},
                        ":previous_allowed": serializer.serialize(current.get("allowed_tools")),
                    }
                )
                condition = (
                    "record_type = :record AND schema_version = :version AND "
                    "tenant_id = :tenant AND owner_id = :owner AND #status = :active AND "
                    "allowed_tools = :previous_allowed"
                )
                if "revision" in current:
                    values[":revision"] = {"N": str(revision)}
                    condition += " AND revision = :revision"
                else:
                    condition += " AND attribute_not_exists(revision)"
                if "consented_tools" in current:
                    values[":previous_consent"] = serializer.serialize(current["consented_tools"])
                    condition += " AND consented_tools = :previous_consent"
                else:
                    condition += " AND attribute_not_exists(consented_tools)"
                self.client.update_item(
                    TableName=self.table_name,
                    Key=key,
                    UpdateExpression="SET allowed_tools = :allowed, consented_tools = :consent, "
                    "tool_max_cost_usd = :ceilings, #catalog = :catalog, revision = :next",
                    ConditionExpression=condition,
                    ExpressionAttributeNames={"#status": "status", "#catalog": "catalog"},
                    ExpressionAttributeValues=values,
                )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise HTTPException(409, "tool policy changed; reload and retry") from None
            raise HTTPException(503, "tool policy unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool policy unavailable") from None
        return {"allowed_tools": allowed_tools, "consented_tools": consented, "tool_max_cost_usd": tool_max_cost_usd, "revision": int(revision + 1)}
