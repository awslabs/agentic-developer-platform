"""Exercise boto3 request/response contracts without contacting AWS."""

import boto3
import pytest
from botocore.stub import Stubber

from src.admin.cognito_service import CognitoService, UserAlreadyExistsError
from src.admin.identity.cognito_sync import CognitoSyncService, cognito_identity
from src.shared.exceptions import ConflictError

POOL = "us-east-1_testpool"
SUB = "dd545950-8d1a-421e-885d-b21312948c49"
USERNAME = "native@example.com"


@pytest.fixture
def cognito():
    client = boto3.client("cognito-idp", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    service = CognitoService(user_pool_id=POOL)
    service._client = client
    with Stubber(client) as stub:
        yield CognitoSyncService(service), stub
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("send_invite", [True, False])
async def test_new_invite_never_resends_and_extracts_real_sub_from_both_aws_shapes(cognito, send_invite):
    sync, stub = cognito
    attributes = [
        {"Name": "email", "Value": USERNAME},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "custom:org_id", "Value": "org"},
        {"Name": "custom:department_id", "Value": "dept"},
        {"Name": "custom:team_id", "Value": "team"},
        {"Name": "custom:role", "Value": "member"},
    ]
    expected = {"UserPoolId": POOL, "Username": USERNAME, "UserAttributes": attributes, "DesiredDeliveryMediums": ["EMAIL"]}
    if not send_invite:
        expected["MessageAction"] = "SUPPRESS"
    stub.add_response(
        "admin_create_user",
        {
            "User": {
                "Username": USERNAME,
                "Attributes": [{"Name": "sub", "Value": SUB}],
                "Enabled": True,
                "UserStatus": "FORCE_CHANGE_PASSWORD",
            }
        },
        expected,
    )
    created = await sync.create_user_and_invite(email=USERNAME, org_id="org", dept_id="dept", team_id="team", send_invite=send_invite)
    assert cognito_identity(created) == (SUB, USERNAME)
    stub.add_response(
        "admin_get_user",
        {
            "Username": USERNAME,
            "UserAttributes": [{"Name": "sub", "Value": SUB}],
            "Enabled": True,
            "UserStatus": "CONFIRMED",
        },
        {"UserPoolId": POOL, "Username": USERNAME},
    )
    looked_up = await sync.verified_user(USERNAME, SUB)
    assert cognito_identity(looked_up) == (SUB, USERNAME)


async def test_existing_username_is_explicit_conflict_and_not_an_empty_success(cognito):
    sync, stub = cognito
    stub.add_client_error("admin_create_user", service_error_code="UsernameExistsException", service_message="Already exists")
    with pytest.raises(UserAlreadyExistsError):
        await sync.create_user_and_invite(email=USERNAME, org_id="org", dept_id="dept", team_id="team")


async def test_cognito_read_does_not_accept_a_different_subject(cognito):
    sync, stub = cognito
    stub.add_response(
        "admin_get_user",
        {"Username": USERNAME, "UserAttributes": [{"Name": "sub", "Value": SUB}]},
        {
            "UserPoolId": POOL,
            "Username": USERNAME,
        },
    )
    with pytest.raises(ConflictError, match="expected_sub"):
        await sync.verified_user(USERNAME, "different-sub")
