"""Predeclared hierarchy targets and ordinary membership restoration baseline."""

import hashlib
import json


def recovery_plan(config):
    fixture = config["hierarchy_lifecycle"]
    identity = json.dumps([config["evaluation_id"], fixture], sort_keys=True)
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return {
        "schema": "hierarchy-lifecycle-v2",
        "evaluation_id": config["evaluation_id"],
        "gateway": config["gateway_url"],
        "fixture": fixture,
        "org_id": "adp-eval-org-" + suffix,
        "department_id": "adp-eval-dept-" + suffix,
        "default_department_id": "adp-eval-org-" + suffix + "-dept-default",
        "default_team_id": "adp-eval-org-" + suffix + "-team-default",
        "team_ids": ["adp-eval-team-a-" + suffix, "adp-eval-team-b-" + suffix],
        "restore": {
            "membership_status": "active",
            "role": "member",
            "team_id": "",
            "teams": [],
        },
    }
