"""Append-only cleanup observations, fenced by the current owner-only permit."""

import copy

from .closed_cleanup_permit import binding_digest, canonical, digest, require


class CleanupJournal:
    def __init__(self, ddb, authority_table, evidence_table, permit, revalidate):
        self.ddb, self.authority_table, self.evidence_table = (
            ddb,
            authority_table,
            evidence_table,
        )
        self.permit, self.revalidate = copy.deepcopy(permit), revalidate
        self.key = "closed-cleanup/" + permit["permit_id"]

    def read(self):
        response = self.ddb.query(
            TableName=self.evidence_table,
            KeyConditionExpression="record_id = :key",
            ExpressionAttributeValues={":key": {"S": self.key}},
            ConsistentRead=True,
            ScanIndexForward=False,
            Limit=1,
        )
        import json

        items = response.get("Items", [])
        if not items:
            return {"sequence": 0, "phase": "new"}
        row = items[0]
        value = json.loads(row["document"]["S"])
        require(row["revision"]["S"] == digest(value))
        require(row["observed_at"]["S"] == f"{value['sequence']:020d}")
        require(row["authority_binding"]["S"] == binding_digest(self.permit))
        return value

    def append(self, previous, **changes):
        self.revalidate()
        require(self.read() == previous)
        value = {
            **copy.deepcopy(previous),
            **changes,
            "sequence": previous["sequence"] + 1,
        }
        row = {
            "record_id": {"S": self.key},
            "observed_at": {"S": f"{value['sequence']:020d}"},
            "document": {"S": canonical(value)},
            "revision": {"S": digest(value)},
            "authority_revision": {"S": digest(self.permit)},
            "authority_binding": {"S": binding_digest(self.permit)},
        }
        # The next immutable sequence key arbitrates concurrent writers. A lost
        # acknowledgement is reconciled by read(); never replay an AWS create.
        self.ddb.transact_write_items(
            TransactItems=[
                {
                    "ConditionCheck": {
                        "TableName": self.authority_table,
                        "Key": {"record_id": {"S": self.permit["permit_id"]}},
                        "ConditionExpression": "revision = :revision",
                        "ExpressionAttributeValues": {
                            ":revision": {"S": digest(self.permit)}
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": self.evidence_table,
                        "Item": row,
                        "ConditionExpression": "attribute_not_exists(record_id)",
                    }
                },
            ]
        )
        self.revalidate()
        require(self.read() == value)
        return value
