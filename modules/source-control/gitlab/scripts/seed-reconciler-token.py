#!/usr/bin/env python3
"""Seed the dedicated reconciler credential from GitLab's local administrator console."""

import argparse
import subprocess

import boto3
from botocore.exceptions import ClientError


def seed(environment, region):
    secrets = boto3.client("secretsmanager", region_name=region)
    name = f"adp/{environment}/gitlab-admin-token"
    try:
        secrets.get_secret_value(SecretId=name)
        print("Reconciler credential already exists; it was not replaced.")
        return
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    # Capture the token in process memory; never emit it to SSM/cloud-init logs.
    result = subprocess.run(
        [
            "gitlab-rails",
            "runner",
            """
user = User.find_by(username: 'adp-home-reconciler')
if user && (!user.admin? || user.email != 'adp-home-reconciler@adp.invalid')
  abort 'Existing service identity does not match'
end
unless user
  user = User.new(username: 'adp-home-reconciler', name: 'ADP home reconciler',
    email: 'adp-home-reconciler@adp.invalid', password: SecureRandom.hex(32), admin: true,
    external: false, can_create_group: true)
  user.assign_personal_namespace(Organizations::Organization.default_organization)
  user.skip_confirmation!
  user.save!
end
token = user.personal_access_tokens.create!(name: 'ADP Product home reconciler', scopes: ['api'], expires_at: 90.days.from_now)
STDOUT.write(token.token)
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value.startswith("glpat-") or any(c.isspace() for c in value):
        raise ValueError("GitLab did not return a valid reconciler credential")
    secrets.put_secret_value(SecretId=name, SecretString=value)
    print("Dedicated reconciler credential stored in Secrets Manager.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["dev", "test", "prod"])
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    seed(args.environment, args.region)
