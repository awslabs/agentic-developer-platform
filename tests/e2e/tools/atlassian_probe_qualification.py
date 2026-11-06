"""Require standalone Identity SDK and provider observations before setup readiness."""

import re
from contextlib import suppress

from tests.e2e.tools.atlassian_oauth_probe import RESOURCE, ProbeBlocked
from tests.e2e.tools.atlassian_probe_ledger import ProbeLedger
from tests.e2e.tools.atlassian_provider_setup import ProviderSetup


def qualify_provider_setup(
    setup,
    ledger,
    provider_reader,
    *,
    owner,
    expected_subject,
    expected_site,
    metadata_sha256,
):
    if (
        not isinstance(setup, ProviderSetup)
        or not isinstance(ledger, ProbeLedger)
        or setup.ledger.lifecycle is not ledger.lifecycle
        or setup.plan.identity.metadata_sha256 != metadata_sha256
        or not isinstance(metadata_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", metadata_sha256)
        or any(
            not isinstance(value, str) or not value
            for value in (owner, expected_subject, expected_site)
        )
        or getattr(provider_reader, "independent", None) is not True
        or not callable(getattr(provider_reader, "observe", None))
    ):
        raise ProbeBlocked(
            "A pinned Identity probe and independent provider reader are required"
        )
    with suppress(Exception):
        prepared = setup.prepare()
        if (
            prepared.get("status") == "ready"
            and prepared.get("mode") == "retained-deployment-client"
            and prepared.get("provider") == setup.plan.identity.provider_name
        ):
            observation = ledger.observe(owner, provider_reader.observe)
            if (
                isinstance(observation, dict)
                and set(observation) == {"resource", "subject", "site"}
                and observation
                == {
                    "resource": RESOURCE,
                    "subject": expected_subject,
                    "site": expected_site,
                }
            ):
                return {
                    "scenario_id": "SCN-01",
                    "status": "candidate",
                    "evidence_kind": "identity-sdk-and-provider-readback",
                    "provider_mode": prepared["mode"],
                    "metadata_sha256": metadata_sha256,
                    "live_status": "unverified",
                }
    raise ProbeBlocked("Standalone Identity and provider observations are incomplete")
