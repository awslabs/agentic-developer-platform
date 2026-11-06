"""Original Product verification cases; report validators are not live producers."""

from dataclasses import dataclass

# Retain the original criteria below for later integration. They are not passes
# in the Product release: live Engineering Insights integration is deferred.
DEFERRED_ISSUES = frozenset({8276})


@dataclass(frozen=True)
class Case:
    fixture_owner: str
    evidence_builder: str
    producers: tuple[str, ...]
    validator: str = ""
    offline_checks: tuple[str, ...] = ()


SCOPES = "tests/e2e/new_ui/test_product_scopes.py::"
RESUME = "tests/e2e/new_ui/test_product_resume.py::"
PROFILE = "tests/e2e/new_ui/test_product_profile.py::"
GUIDANCE = "tests/e2e/new_ui/test_product_guidance.py::"
CATALOG = "tests/e2e/product_onboarding/test_v12.py::"
V06_LIVE = "tests/e2e/product_onboarding/test_product_v06_live.py::"
V07_LIVE = "tests/e2e/product_onboarding/test_product_v07_live.py::"


def case(version, owner, *producers, validator=""):
    return Case(owner, f"tests/e2e/product_onboarding/product_v{version}_evidence.py",
                producers, validator)


CASES = {
    8250: {
        "AC-01": case("04", "product_scope_fixture", SCOPES + "test_scope_isolation_api",
                      SCOPES + "test_scope_isolation", "case:PR04-C01"),
        "AC-02": case("04", "product_scope_fixture", SCOPES + "test_scope_denials_api",
                      SCOPES + "test_scope_denials", "case:PR04-C02"),
        "AC-03": case("04", "product_scope_fixture", "case:PR04-C03"),
    },
    8252: {
        "AC-01": case("05", "product_resume_fixture", RESUME + "test_resume_ui_to_cli",
                      RESUME + "test_resume_cli_to_ui"),
        "AC-02": case("05", "product_resume_fixture", RESUME + "test_progress_math"),
        "AC-03": case("05", "product_resume_fixture", RESUME + "test_resume_recovery"),
    },
    8256: {
        # The deployed V06_LIVE producers run real discovery, binding, verification,
        # independent readback and cleanup against the run-owned Project and apply the
        # reviewed SCN validators to the reports they assemble, so an incomplete, shared
        # or uncleaned observation fails the producer instead of being published. The
        # report-only test_v06.py checks are therefore not the selected evidence.
        "AC-01": case("06", "owned_sources_run", V06_LIVE + "live_sources_reuse"),
        "AC-02": case("06", "owned_sources_run", V06_LIVE + "live_source_status"),
        "AC-03": case("06", "owned_sources_run", V06_LIVE + "live_source_recovery"),
    },
    8258: {
        "AC-01": case("07", "product_resume_provider", *(GUIDANCE + f"test_guided_journeys[{start}]"
                      for start in ("idea", "existing_assets", "running_product"))),
        # The deployed V07_LIVE producers perform every guidance turn through the
        # installed CLI and the served API, read the metered calls back from the
        # independent authorized usage journal, select and then revoke a real
        # document source, and resume the pending proposals in the browser, the CLI
        # and the API before applying the reviewed SCN report checks to the evidence
        # they assemble. The report-only test_v07.py checks are therefore not the
        # selected evidence: a report validator is not a deployed test producer.
        "AC-02": case("07", "owned_guidance_run", V07_LIVE + "live_guidance_effects"),
        "AC-03": case("07", "owned_guidance_run", V07_LIVE + "live_guidance_resume"),
    },
    8261: {
        "AC-01": case("08", "owned_handoff_run", "tests/e2e/product_onboarding/test_v08.py::live_admin_handoff"),
        "AC-02": case("08", "owned_repository_run", "tests/e2e/product_onboarding/test_v08.py::live_repository_recovery"),
        "AC-03": case("08", "owned_provider_run", "tests/e2e/product_onboarding/test_v08.py::live_provider_cleanup"),
    },
    8264: {
        "AC-01": case("09", "owned_financial_run", "tests/e2e/product_onboarding/test_v09.py::live_financial_selective_import"),
        "AC-02": case("09", "owned_financial_run", "tests/e2e/product_onboarding/test_v09.py::live_financial_denials"),
        "AC-03": case("09", "owned_financial_run", "tests/e2e/product_onboarding/test_v09.py::live_financial_resume"),
    },
    8267: {
        "AC-01": case("10", "owned_cli_run", "tests/e2e/product_onboarding/test_v10.py::live_cli_full_journeys"),
        "AC-02": case("10", "owned_cli_run", "tests/e2e/product_onboarding/test_v10.py::live_cli_peer_unavailable"),
        "AC-03": case("10", "owned_cli_run", "tests/e2e/product_onboarding/test_v10.py::live_cli_release_evidence"),
    },
    8268: {
        "AC-01": case("11", "product_resume_provider", *(PROFILE + f"test_product_full_journey[{start}]"
                      for start in ("idea", "existing_assets", "running_product"))),
        "AC-02": case("11", "product_resume_provider", RESUME + "test_v11_cross_client_unavailable"),
        "AC-03": case("11", "product_resume_provider", PROFILE + "test_v11_release_regressions"),
    },
    8273: {
        "AC-01": Case("catalog_live_provider", "tests/e2e/product_onboarding/product_catalog_evidence.py",
                      (CATALOG + "live_catalog_membership", "tests/e2e/new_ui/test_product_catalog.py::test_catalog_membership")),
        "AC-02": Case("catalog_live_provider", "tests/e2e/product_onboarding/product_catalog_evidence.py",
                      (CATALOG + "live_catalog_history_access",)),
        "AC-03": Case("catalog_live_provider", "tests/e2e/product_onboarding/product_catalog_evidence.py",
                      (CATALOG + "live_catalog_recovery",)),
    },
    8276: {
        "AC-01": Case("owned_insights_run", "modules/insights/scripts/v13_packet.py", (),
                      offline_checks=("modules/insights/tests/test_v13_registry.py::V13OfflineScenarios::test_registry_lineage",)),
        "AC-02": Case("owned_insights_run", "modules/insights/scripts/v13_packet.py", (),
                      offline_checks=("modules/insights/tests/test_v13_registry.py::V13OfflineScenarios::test_lineage_deduplication",)),
        "AC-03": Case("owned_insights_run", "modules/insights/scripts/v13_packet.py", (),
                      offline_checks=("modules/insights/tests/test_v13_registry.py::V13OfflineScenarios::test_lineage_access",)),
        "AC-04": Case("owned_insights_run", "modules/insights/scripts/v13_packet.py",
                      ("tests/e2e/product_onboarding/test_v13_live.py::live_insights_cli_checkpoint",),
                      offline_checks=("modules/insights/tests/test_v13_registry.py::V13OfflineScenarios::test_insights_cli_checkpoint",
                                      "modules/insights/tests/test_v13_registry.py::V13OfflineScenarios::test_insights_cli_revocation")),
    },
}
