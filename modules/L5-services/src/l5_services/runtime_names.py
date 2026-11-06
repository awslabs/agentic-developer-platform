"""Canonical names and scope parsing for independently installed L5 roots."""

import re


SERVICES = frozenset({"security", "devops", "transform", "well_architected", "finops"})


def functions(environment, service="security"):
    if environment not in {"dev", "test", "prod"}:
        raise ValueError("Unsupported protected environment")
    if service not in SERVICES:
        raise ValueError("Unsupported L5 service")
    suffix = "" if service == "security" else "-" + service.replace("_", "-")
    return (f"adp-{environment}-l5{suffix}-worker", f"adp-{environment}-l5{suffix}-repair")


def queue_scope(arn):
    match = re.fullmatch(
        r"arn:aws:sqs:([a-z0-9-]+):([0-9]{12}):adp-(dev|test|prod)-l5"
        r"(?:-(devops|transform|well-architected|finops))?-work", arn if isinstance(arn, str) else ""
    )
    if match is None:
        raise ValueError("Expected the exact selected L5 service work queue")
    region, account, environment, service = match.groups()
    return region, account, environment, (service or "security").replace("-", "_")
