"""Owned ordinary policy targets retained before any metadata mutation."""


def recovery_plan(config):
    fixture = config["budget_lifecycle"]
    return {
        "version": 1,
        "purpose": "budget_lifecycle",
        "evaluation_id": config["evaluation_id"],
        "gateway": config["gateway_url"],
        "fixture": fixture,
        "budgets": [
            {"usage": usage, "period": period}
            for usage in ("personal", "cloud-agents")
            for period in ("daily", "weekly", "monthly")
        ],
        "rate_limit": {
            "scope": "user",
            "target": fixture["ordinary_canonical_user_id"],
        },
        "baseline": "Every selected override must be absent before the first write",
        "cleanup": "Delete only acknowledged unchanged revisions; never reset usage. Unknown delivery requires operator reconciliation of retained journal.",
    }
