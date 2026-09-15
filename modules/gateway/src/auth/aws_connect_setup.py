"""Portable CloudFormation handoff for a saved *personal* AWS connection.

Issue #5182. The connect flow returns a signed console URL, which expires and
cannot be given to somebody else. A user who cannot create IAM resources needs
files their AWS administrator can apply, and needs them to carry the **same**
ExternalId and user session tag the saved credential already expects — a
regenerated pair would make the role ADP verifies against a different role.

Separate from :mod:`src.auth.routing_setup` on purpose: that package describes a
shared Bedrock routing destination and sends the reader to Model Access. This one
is a personal read-only connection, and saying so is the point — connecting an
account must not read as opting into shared inference routing.
"""

import base64
import io
import json
import shlex
import zipfile
from urllib.parse import parse_qs, urlsplit

#: Filenames in the package. The CLI validates against exactly this set.
PACKAGE_FILES = ("template.yaml", "parameters.json", "README.md")


def connect_setup_download(*, launch_url: str, account_id: str, role_arn: str, region: str, template: str) -> str:
    """Base64 ZIP holding the template, its parameters and apply instructions.

    Parameters are read back out of ``launch_url`` rather than rebuilt, so the
    downloaded package and the console Quick-Create launch cannot disagree about
    the ExternalId or the session tag.
    """
    query = parse_qs(urlsplit(launch_url).fragment.split("?", 1)[1])
    parameters = [
        {"ParameterKey": key.removeprefix("param_"), "ParameterValue": values[0]} for key, values in query.items() if key.startswith("param_")
    ]
    stack_name = query["stackName"][0]
    cli_region = shlex.quote(region)
    cli_stack = shlex.quote(stack_name)
    instructions = f"""# ADP personal AWS connection

Target AWS account: {account_id}
Region: {region}
Expected role ARN: {role_arn}

An AWS administrator with CloudFormation and IAM role/policy creation permissions
can apply these files. ADP access alone does not grant those AWS permissions.

## AWS Console

1. Sign in to account {account_id} and open CloudFormation in {region}.
2. Create a stack with template.yaml. Enter the values from parameters.json.
3. Acknowledge creation of a named IAM role and wait for CREATE_COMPLETE.
4. Return to ADP > Settings > Credentials and verify the connection, or run
   `adp aws verify` with this connection's ID.

## AWS CLI

From this extracted directory, using credentials for account {account_id}:

```sh
aws sts get-caller-identity
aws cloudformation create-stack --region {cli_region} --stack-name {cli_stack} \\
  --template-body file://template.yaml --parameters file://parameters.json \\
  --capabilities CAPABILITY_NAMED_IAM
aws cloudformation wait stack-create-complete --region {cli_region} --stack-name {cli_stack}
```

Check the account returned by the first command before creating the stack.
The stack creates one read-only role and its trust policy. Keep the parameter
values unchanged so ADP can assume the expected role: the role trusts ADP only
for the one ADP user named in the UserSessionTag parameter, and only with this
ExternalId. This package therefore contains the connection's ExternalId — share
it only with the AWS administrator.

The downloaded files do not expire. ADP verifies the saved account and role; it
does not need your AWS login or access keys. This connection lets ADP read that
account on your behalf. It does not route anyone's Bedrock model calls to it —
that is a separate, separately authorized decision.
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("template.yaml", template)
        bundle.writestr("parameters.json", json.dumps(parameters, indent=2) + "\n")
        bundle.writestr("README.md", instructions)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
