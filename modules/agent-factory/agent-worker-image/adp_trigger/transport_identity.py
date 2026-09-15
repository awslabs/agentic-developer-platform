"""Keep platform control traffic on IRSA after customer AWS credentials load."""

import os


def worker_credentials(session):
    # Operations tasks replace AWS_ACCESS_KEY_ID et al. with customer creds.
    # Those belong to task tools, not the platform authority transport. Use the
    # refreshable web-identity provider directly, without mutating process env.
    if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() == "true" and os.environ.get("AWS_ROLE_ARN"):
        return session.get_component("credential_provider").get_provider("assume-role-with-web-identity").load()
    return session.get_credentials()
