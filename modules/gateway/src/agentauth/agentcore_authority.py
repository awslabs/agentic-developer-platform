"""Server-owned memory scope shared by gateway adapters and processing."""

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.orchestration.chat_data_migration import _owns_context_row


def shared_owner_header(table, launch, *, require_shared=True):
    header = table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
    if not header or not _owns_context_row(header, (launch.tenant_id, launch.team_id, launch.user_id)):
        raise ChatAuthorizationRefusedError("memory owner refused")
    if header.get("memoryPrivacy") != "shared" or header.get("memoryConsent") is not True:
        if require_shared:
            raise ChatAuthorizationRefusedError("memory sharing refused")
        return None
    return header
