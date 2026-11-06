"""Bind the foundation gateway job to its accepted native release and lease."""

import os

from guard_release import GuardRefused, authorize, require


def check(environment, opener=None):
    require(
        environment.get("ADP_RELEASE_GROUP") == "atlassian-foundation"
        and environment.get("ADP_ENVIRONMENT") == "dev"
        and environment.get("ADP_MANUAL_SOURCE", "") == ""
        and environment.get("ADP_PRODUCT_CASES", "none") == "none"
        and environment.get("ADP_PROOF_PHASE", "none") == "none"
        and environment.get("ADP_PRODUCT_ROLLBACK", "false") == "false"
    )
    authorize(
        {
            **environment,
            "ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN": environment.get("ADP_FOUNDATION_CONNECTION_ROLE_ARN", ""),
            "ADP_TOOLS_DEPLOY_ROLE_ARN": environment.get("ADP_DEPLOY_ROLE_ARN", ""),
            "ADP_TOOLS_EVAL_GATEWAY_URL": environment.get("ADP_FOUNDATION_GATEWAY_URL", ""),
        },
        opener,
        workflow="gateway-deploy.yml",
    )


if __name__ == "__main__":
    try:
        check(os.environ)
    except GuardRefused:
        raise SystemExit("native_release_guard_refused") from None
