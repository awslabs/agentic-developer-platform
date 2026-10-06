"""Sign exactly the bytes sent to the selected API Gateway using current credentials."""

import botocore.auth
import botocore.awsrequest


def signed_headers(credentials, url, encoded, region):
    request = botocore.awsrequest.AWSRequest(
        method="POST",
        url=url,
        data=encoded,
        headers={"Content-Type": "application/json"},
    )
    botocore.auth.SigV4Auth(
        credentials.get_frozen_credentials(), "execute-api", region
    ).add_auth(request)
    return dict(request.headers)
