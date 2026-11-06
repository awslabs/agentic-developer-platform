"""Match an API Gateway authenticated caller to one configured IAM role.

Callers must first verify the private edge proof. API Gateway's userArn is an
STS session ARN for role credentials, and STS omits the IAM role's path. Role
names are unique within an AWS account; the account and full name must match.
"""

import re


def matches_role(caller, expected):
    if not isinstance(caller, str) or not isinstance(expected, str):
        return False
    role = re.fullmatch(
        r"arn:(aws(?:-us-gov|-cn)?):iam::([0-9]{12}):role/(?:[A-Za-z0-9+=,.@_-]+/)*([A-Za-z0-9+=,.@_-]{1,64})", expected
    )
    if role is None:
        return False
    if caller == expected:
        return True
    session = re.fullmatch(
        r"arn:(aws(?:-us-gov|-cn)?):sts::([0-9]{12}):assumed-role/([A-Za-z0-9+=,.@_-]{1,64})/([A-Za-z0-9+=,.@_-]{2,64})",
        caller,
    )
    return session is not None and session.groups()[:3] == role.groups()
