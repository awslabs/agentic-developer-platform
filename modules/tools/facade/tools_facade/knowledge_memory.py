"""Verify memory completion evidence before settling an owned operation."""

import hashlib
import json

from tools_facade.encoding import canonical

MEMORY_TOOLS = frozenset({"knowledge___remember", "knowledge___experience"})


def completion_matches(operation, result):
    try:
        payload = result["result"]
        proof = payload["_meta"]["adp_knowledge_memory"]
        content = payload["content"]
        if (
            payload.get("isError")
            or type(proof) is not dict
            or set(proof)
            != {
                "schema_version",
                "tool",
                "principal",
                "receipt_id",
                "arguments_sha256",
                "request_id",
                "execution_id",
                "result_sha256",
            }
            or operation["tool"]["S"] not in MEMORY_TOOLS
            or not (operation["binding"]["S"] == "human" and operation.get("memory_contract") == {"S": "1"}
                    or operation["binding"]["S"].startswith("run:")
                    and operation.get("memory_contract") == {"S": "host"})
            or type(content) is not list
            or len(content) != 1
            or content[0]["type"] != "text"
        ):
            return False
        value = json.loads(content[0]["text"])
        return (
            proof["schema_version"] == "1.0"
            and proof["tool"] == operation["tool"]["S"]
            and proof["principal"]
            == {
                "tenant_id": operation["tenant_id"]["S"],
                "user_id": operation["owner_id"]["S"],
                "team_id": operation["team_id"]["S"],
            }
            and proof["receipt_id"] == operation["receipt_id"]["S"]
            and proof["arguments_sha256"] == operation["request_digest"]["S"]
            and proof["request_id"] == operation["request_id"]["S"]
            and proof["execution_id"] == operation["operation_execution_id"]["S"]
            and proof["result_sha256"] == hashlib.sha256(canonical(value)).hexdigest()
        )
    except (KeyError, TypeError, ValueError):
        return False
