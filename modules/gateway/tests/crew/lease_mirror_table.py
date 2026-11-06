"""A moto context table that behaves like DynamoDB under the gateway role (#8223 cause 43).

Two things moto accepts that the real service and the real role refuse, both of which let a
green suite ship a 500 and a silent no-op to dev on 2026-10-21:

* DynamoDB rejects an ``ExpressionAttributeValues`` entry the expression never uses
  (``ValidationException``). ``publish_host_lease`` carried ``:now``/``:provisioning`` on a branch
  whose condition did not use them, so every ``GET /crew/target`` was a 500.
* The gateway role's ``CrewHostLeaseWrite`` grant on ``crew#*`` keys is PutItem + DeleteItem only
  (``modules/agent-factory/infra/gateway-intake-access.tf``). The fence cleared the mirror with
  UpdateItem, which was AccessDenied, so the stale lease stayed.

Wrap the moto table in this and both become test failures.
"""

import re

from botocore.exceptions import ClientError

_PLACEHOLDER = re.compile(r":[A-Za-z0-9_]+")


def _client_error(operation, code, message):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class GatewayRoleTable:
    """Delegates to a moto table; refuses what DynamoDB or the gateway role would refuse."""

    def __init__(self, table):
        self._table = table

    def __getattr__(self, name):
        return getattr(self._table, name)

    @staticmethod
    def _check_values(operation, kwargs):
        expressions = " ".join(
            str(kwargs.get(key, "")) for key in ("ConditionExpression", "UpdateExpression", "KeyConditionExpression", "FilterExpression")
        )
        used = set(_PLACEHOLDER.findall(expressions))
        unused = sorted(set(kwargs.get("ExpressionAttributeValues", {})) - used)
        if unused:
            raise _client_error(
                operation,
                "ValidationException",
                "1 validation error detected: Value provided in ExpressionAttributeValues unused in expressions: keys: {" + ", ".join(unused) + "}",
            )

    def put_item(self, **kwargs):
        self._check_values("PutItem", kwargs)
        return self._table.put_item(**kwargs)

    def delete_item(self, **kwargs):
        self._check_values("DeleteItem", kwargs)
        return self._table.delete_item(**kwargs)

    def update_item(self, **kwargs):
        self._check_values("UpdateItem", kwargs)
        if str(kwargs.get("Key", {}).get("PK", "")).startswith("crew#"):
            raise _client_error(
                "UpdateItem",
                "AccessDeniedException",
                "User: arn:aws:sts::000000000000:assumed-role/adp-test-role-gateway-service/test is not authorized to perform: "
                "dynamodb:UpdateItem on resource: lease mirror (CrewHostLeaseWrite grants PutItem and DeleteItem on crew#* keys)",
            )
        return self._table.update_item(**kwargs)
