"""Portable CloudFormation handoff for a saved Bedrock destination.

Use the same template and parameters as Quick Create. The ZIP remains usable
after the signed console URL expires; no AWS credentials are needed to download it.
"""

import base64
import io
import json
import shlex
import zipfile
from urllib.parse import parse_qs, urlsplit


def routing_setup_download(*, launch_url: str, account_id: str, role_arn: str, region: str, template: str) -> str:
    query = parse_qs(urlsplit(launch_url).fragment.split("?", 1)[1])
    parameters = [
        {"ParameterKey": key.removeprefix("param_"), "ParameterValue": values[0]} for key, values in query.items() if key.startswith("param_")
    ]
    stack_name = query["stackName"][0]
    cli_region = shlex.quote(region)
    cli_stack = shlex.quote(stack_name)
    instructions = f"""# ADP Bedrock destination

Target AWS account: {account_id}
Region: {region}
Expected role ARN: {role_arn}

An AWS administrator with CloudFormation and IAM role/policy creation permissions
can apply these files. ADP access alone does not grant those AWS permissions.

## AWS Console

1. Sign in to account {account_id} and open CloudFormation in {region}.
2. Create a stack with template.yaml. Enter the values from parameters.json.
3. Acknowledge creation of a named IAM role and wait for CREATE_COMPLETE.
4. Return to ADP > Model Access > Bedrock account routing, find this pending
   destination, and choose Continue setup, then Verify & Save.

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
The stack creates only a Bedrock invocation role and its trust policy. Keep the
parameter values unchanged so ADP can assume the expected role. This package
contains the destination's ExternalId; share it only with the AWS administrator.
The downloaded files do not expire. ADP verifies the saved account and role;
it does not need your AWS login or access keys. A routing rule can use this
destination only after verification succeeds.
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("template.yaml", template)
        bundle.writestr("parameters.json", json.dumps(parameters, indent=2) + "\n")
        bundle.writestr("README.md", instructions)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
