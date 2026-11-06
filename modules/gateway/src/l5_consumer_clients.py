"""Human use of admitted WA copies preserves the original authenticated actor."""

from fastapi import HTTPException
from l5_services.publication.client import WellArchitectedConsumerClient

from src.internal.l5_consumer_routes import human_consumer_request


class HumanWAConsumer(WellArchitectedConsumerClient):
    def __init__(self, *, actor, sessions, scope, destination, contract):
        super().__init__(None, "current-human-authority", scope=scope, destination=destination, peer_admission={**contract, "scope": scope})
        self.actor, self.sessions = actor, sessions

    async def reuse_receipt(self, publication, *, task_id, actor_id, citation, retrieval_receipt_id=None):
        if actor_id != self.actor.user_id:
            raise ValueError("consumer actor does not match authenticated user")
        arguments = {"actor": self.actor, "publication": publication, "task_id": task_id, "sessions": self.sessions}
        try:
            result = await human_consumer_request(**arguments, read_only=True)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            await human_consumer_request(**arguments, read_only=False)
            result = await human_consumer_request(**arguments, read_only=True)
        expected = {
            "scope": publication.payload["scope"],
            "result_ref": publication.payload["result_ref"],
            "contribution_id": publication.publication_id,
            "task_id": task_id,
            "actor_id": actor_id,
            "citation": citation,
        }
        if retrieval_receipt_id is not None:
            expected["retrieval_receipt_id"] = retrieval_receipt_id
        if any(result.get(key) != value for key, value in expected.items()) or not result.get("receipt_id"):
            raise ValueError("consumer receipt differs from requested task use")
        return result
