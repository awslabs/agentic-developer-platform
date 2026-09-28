"""The pinned SkyPilot image's vulnerable bundled copies must stay described — Issue #5602 (S03).

## What the 2026-09-21 scan found, and why a version number was not enough

Grype reported five high matches against the image `releases/superplane.lock.yaml` pins. Four
of the five were not top-level packages that a `pip install --upgrade` would reach:

| Reported package | Where it actually lives |
| --- | --- |
| `jaraco-context` 5.3.0 | `setuptools/_vendor/` — setuptools' private copy |
| `wheel` 0.45.1 | `setuptools/_vendor/` — setuptools' private copy |
| `jackson-core` 2.16.1 | inside `ray/jars/ray_dist.jar` — a Java library in a Python wheel |
| `cryptography` 43.0.3 | top-level `site-packages` |

The image already demonstrated the trap: the **top-level** `wheel` was 0.46.3, which is the
patched version, while the copy vendored inside setuptools was still 0.45.1 and still reported.
Upgrading the obvious package would have looked like a fix and changed nothing.

## What these tests do

They are not a scanner. A scanner runs in CI against a built image; these tests protect the
*written record* that the S03 disposition rests on, so a later re-pin cannot quietly invalidate
it. Specifically they assert that for every bundled finding the record names the containing
distribution — the thing that actually has to move — rather than only the vulnerable package.

`docs/security/runs/2026-09-21/S03-disposition.md` states the reasoning; this file
keeps the machine-readable half of it (`skypilot_vendored_packages.json`) honest:

*   Every entry names an advisory, the reported package, and the distribution that bundles it.
*   The two setuptools-vendored findings name the containing-version threshold. The derived
    candidate must move that containing distribution to the threshold and clear both matches.
*   Baseline and candidate are separate complete observations. The observation matching the
    lock is selected, and a digest-only relabel cannot make stale baseline versions describe
    the candidate — the same digest coupling `test_skypilot_startup_contract.py` enforces for
    the image facts.

## What is NOT claimed here

These tests read files. They do not scan an image, do not prove the current image still
contains these versions, and do not establish exploitability — the reachability argument and
its executable reproduction live in the disposition and in
`docs/security/runs/2026-09-21/evidence/S03-jaraco-context-traversal-check.py`. Passing here
means the record is complete and still describes the pinned image, not that the image is clean.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = MODULE_ROOT / "releases" / "superplane.lock.yaml"
VENDORED_FACTS = Path(__file__).resolve().parent / "skypilot_vendored_packages.json"
S03_EVIDENCE = MODULE_ROOT.parents[2] / "docs/security/runs/2026-09-21/evidence"
LIVE_HARNESS = S03_EVIDENCE / "S03-live-validation.sh"
LIVE_SUPERVISOR = S03_EVIDENCE / "S03-skypilot-supervisor.sh"
CONTROLLER_PROBE = S03_EVIDENCE / "S03-controller-client-check.go"
BUILD_RECIPE = MODULE_ROOT / "images/skypilot/build_oci.py"
BUILD_PROVENANCE = S03_EVIDENCE / "S03-derived-image-provenance.json"
DERIVED_IMAGE_FACTS = S03_EVIDENCE / "S03-skypilot_image_facts-derived.json"
UPSTREAM_COMPARISON = S03_EVIDENCE / "S03-grype-high-comparison.json"
GRYPE_CONFIG = MODULE_ROOT.parents[2] / ".grype.yaml"
LEARNING_RECORD = (
    MODULE_ROOT.parents[2] / "agent_learning/2026-09-21-issue-5602-learnings.md"
)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# The five high matches the scan attributed to this image, from
# docs/security/runs/2026-09-21/findings.json (run 35546057969,
# artifact superplane/grype/superplane-skypilot-api.sarif).
REPORTED_ADVISORIES = {
    "CVE-2023-36632",
    "GHSA-58pv-8j8x-9vj2",
    "GHSA-72hv-8253-57qq",
    "GHSA-8rrh-rw8j-w5fx",
    "GHSA-r6ph-v2qm-q3c2",
}


@pytest.fixture(scope="module")
def record() -> dict:
    return json.loads(VENDORED_FACTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def findings(record) -> dict:
    return {entry["advisory"]: entry for entry in record["findings"]}


@pytest.fixture(scope="module")
def candidate_findings(record) -> dict:
    return {entry["advisory"]: entry for entry in record["candidate"]["findings"]}


@pytest.fixture(scope="module")
def locked_digest() -> str:
    lock = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))
    return str(lock["images"]["skypilot-api"])


def test_live_acceptance_harness_has_valid_shell_syntax():
    for script, shell in ((LIVE_HARNESS, "bash"), (LIVE_SUPERVISOR, "sh")):
        assert script.is_file(), f"missing reproducible S03 live evidence: {script}"
        subprocess.run([shell, "-n", str(script)], check=True)


def test_live_acceptance_harness_pins_and_verifies_the_candidate(record):
    harness = LIVE_HARNESS.read_text(encoding="utf-8")
    candidate = record["candidate"]
    assert f'SKY_DIGEST="{candidate["observed_in_digest"]}"' in harness
    assert "S03_SKYPILOT_REPOSITORY:?" in harness
    assert 'SKY_IMAGE="${SKY_REPOSITORY}@${SKY_DIGEST}"' in harness
    assert 'docker pull --platform linux/amd64 "${SKY_IMAGE}"' in harness
    assert "docker image inspect" in harness
    assert "RepoDigests" in harness
    assert "{{.Architecture}}" in harness


def test_live_acceptance_harness_exercises_the_missing_runtime_evidence():
    harness = LIVE_HARNESS.read_text(encoding="utf-8")
    required_fragments = (
        "POSTGRES_IMAGE=",
        "SKYPILOT_DB_CONNECTION_URI=${DATABASE_URI}",
        "IS_SKYPILOT_SERVER=true",
        "python3 -m app.skypilot_proxy",
        "expect_status 401",
        'go run "${GO_PROBE}"',
        "/users/create",
        "SELECT type FROM users",
        "kill -TERM",
        "expect_status 503",
        "s03-server.restart",
        "SECOND_SERVER_PID",
        "assert_test_user_response",
        '[[ "${required_schema_count}" == "3" ]]',
    )
    missing = [fragment for fragment in required_fragments if fragment not in harness]
    assert not missing, f"S03 live harness no longer proves: {missing}"


def test_live_probe_uses_the_maintained_authenticated_controller_client():
    probe = CONTROLLER_PROBE.read_text(encoding="utf-8")
    assert (
        '"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"'
        in probe
    )
    for call in (
        "skypilot.NewClient(",
        "skypilot.WithServiceToken(serviceToken)",
        "client.Health(ctx)",
        "client.Status(ctx)",
    ):
        assert call in probe
    assert 'health.Version != "0.12.3"' in probe


def test_live_harness_generates_and_redacts_its_secrets():
    harness = LIVE_HARNESS.read_text(encoding="utf-8")
    assert "secrets.token_urlsafe" in harness
    assert "secrets.token_hex" in harness
    assert "<redacted>" in harness
    assert "adp.security-run" in harness
    assert "docker container inspect" in harness
    assert "set -x" not in harness


@pytest.mark.parametrize("observation", ["findings", "candidate.findings"])
def test_every_reported_advisory_is_recorded(record, observation):
    """No finding may be silently dropped from the record.

    The disposition has to account for all five. Omitting one would make the record look
    resolved while a reported finding had simply gone unmentioned.
    """
    entries = (
        record["findings"]
        if observation == "findings"
        else record["candidate"]["findings"]
    )
    advisories = {entry["advisory"] for entry in entries}
    missing = REPORTED_ADVISORIES - advisories
    assert not missing, (
        f"the 2026-09-21 scan reported {sorted(missing)} against the pinned SkyPilot image, "
        f"but {VENDORED_FACTS.name} does not record them"
    )


@pytest.mark.parametrize("observation", ["findings", "candidate.findings"])
def test_no_invented_advisories(record, observation):
    """The record describes this scan, not a wishlist.

    An extra advisory here would be an unsourced claim: there is no evidence locator for it in
    findings.json, so a reader could not check it.
    """
    entries = (
        record["findings"]
        if observation == "findings"
        else record["candidate"]["findings"]
    )
    advisories = {entry["advisory"] for entry in entries}
    extra = advisories - REPORTED_ADVISORIES
    assert not extra, (
        f"{sorted(extra)} are recorded but were not among the five high matches the scan "
        "attributed to this image; add the evidence locator or remove them"
    )


def test_the_record_describes_the_locked_image(record, locked_digest):
    """The record is tied to the digest it was observed against.

    Package versions are a property of one specific image. If the lock moves, this record
    describes an image nobody deploys any more, so it must be re-derived rather than inherited.
    """
    assert DIGEST_RE.match(locked_digest), (
        f"lock digest is malformed: {locked_digest!r}"
    )
    observations = [record, record["candidate"], *record.get("observations", [])]
    matching = [
        item for item in observations if item["observed_in_digest"] == locked_digest
    ]
    assert len(matching) == 1, (
        "skypilot_vendored_packages.json must contain exactly one complete package observation "
        f"for the locked digest {locked_digest}; found {len(matching)}"
    )
    assert {item["advisory"] for item in matching[0]["findings"]} == REPORTED_ADVISORIES
    for item in matching[0]["findings"]:
        assert item["package"] and item["version"]
        if item["bundled"]:
            assert item["bundled_in"]["name"] and item["bundled_in"]["version"]


def test_candidate_has_exact_digest_build_scan_and_inventory_provenance(record):
    candidate = record["candidate"]
    provenance = json.loads(BUILD_PROVENANCE.read_text(encoding="utf-8"))
    recipe = BUILD_RECIPE.read_text(encoding="utf-8")
    assert DIGEST_RE.match(candidate["observed_in_digest"])
    assert candidate["observed_in_reference"].startswith("oci-layout:")
    assert provenance["output"]["manifest_digest"] == candidate["observed_in_digest"]
    assert candidate["build"]["recipe"] == str(
        BUILD_RECIPE.relative_to(MODULE_ROOT.parents[2])
    )
    assert candidate["observed_in_digest"] in recipe
    assert (
        candidate["build"]["setuptools_wheel_sha256"].removeprefix("sha256:") in recipe
    )
    assert ".wh..wh..opq" in recipe
    assert candidate["scan"]["tool"] == "grype"
    assert candidate["scan"]["version"] == "0.80.2"
    assert (
        candidate["scan"]["database_checksum"]
        == provenance["scan"]["database_checksum"]
    )
    assert candidate["inventory"]["tool"] == "syft"
    assert candidate["inventory"]["version"] == "1.11.1"


def test_candidate_facts_are_digest_coupled_to_the_rebuild(record):
    facts = json.loads(DERIVED_IMAGE_FACTS.read_text(encoding="utf-8"))
    candidate = record["candidate"]
    assert facts["image_index_digest"] == candidate["observed_in_digest"]
    assert facts["amd64_manifest_digest"] == candidate["observed_in_digest"]
    assert facts["config_blob_digest"] == candidate["build"]["config_digest"]
    assert facts["skypilot_version"] == "0.12.3"


def test_learning_record_recommends_the_complete_repaired_handoff(record):
    learning = LEARNING_RECORD.read_text(encoding="utf-8")
    normalized_learning = " ".join(learning.split())
    candidate = record["candidate"]
    assert candidate["observed_in_digest"] in learning
    for repaired_fact in (
        "setuptools 81.0.0",
        "All four remediable story findings are fixed",
        "authenticated controller connectivity",
        "PostgreSQL persistence",
        "restart",
    ):
        assert repaired_fact in normalized_learning
    assert (
        "Upstream SkyPilot 0.12.3 alone is therefore not the replacement image"
        in normalized_learning
    )
    assert "two findings documented as unreachable" not in normalized_learning
    assert "re-pin to SkyPilot 0.12.3 recommended" not in normalized_learning


def test_candidate_fixes_every_remediable_story_finding(findings, candidate_findings):
    fixed_advisories = {
        "GHSA-58pv-8j8x-9vj2",
        "GHSA-8rrh-rw8j-w5fx",
        "GHSA-72hv-8253-57qq",
        "GHSA-r6ph-v2qm-q3c2",
    }
    for advisory in fixed_advisories:
        baseline = findings[advisory]
        candidate = candidate_findings[advisory]
        assert candidate["status"] == "fixed"
        assert candidate["scanner_match"] is False
        assert candidate["version"] != baseline["version"]
        if candidate["bundled"]:
            assert (
                candidate["bundled_in"]["version"] != baseline["bundled_in"]["version"]
            )


def test_candidate_only_retains_the_disputed_story_match(findings, candidate_findings):
    advisory = "CVE-2023-36632"
    candidate = candidate_findings[advisory]
    assert candidate["status"] == "disputed_not_applicable"
    assert candidate["scanner_match"] is True
    assert candidate["version"] == findings[advisory]["version"]


def test_story_findings_are_not_hidden_by_global_grype_ignores():
    config = yaml.safe_load(GRYPE_CONFIG.read_text(encoding="utf-8"))
    ignored = {entry["vulnerability"] for entry in config.get("ignore", [])}
    assert not REPORTED_ADVISORIES & ignored


def test_derived_layer_introduces_no_new_non_story_critical_high(record):
    comparison = json.loads(UPSTREAM_COMPARISON.read_text(encoding="utf-8"))
    upstream = {item["id"] for item in comparison["0.12.3"]["high_critical"]}
    derived = {
        item["advisory"]
        for item in record["candidate"]["additional_critical_high_findings"]
    }
    assert derived <= upstream


def test_every_unsuppressed_candidate_critical_high_has_a_specific_disposition(record):
    candidate = record["candidate"]
    provenance = json.loads(BUILD_PROVENANCE.read_text(encoding="utf-8"))
    additional = candidate["additional_critical_high_findings"]
    expected = {
        match["advisory"]
        for match in provenance["scan"]["all_critical_high_matches"]
        if match["advisory"] != "CVE-2023-36632"
    }
    assert {item["advisory"] for item in additional} == expected
    assert candidate["scan"]["critical_count"] == 1
    assert candidate["scan"]["high_count"] == 13
    for finding in additional:
        assert finding["severity"] in {"Critical", "High"}
        assert finding["occurrences"]
        assert len(finding["reason"]) > 100


@pytest.mark.parametrize("observation", ["findings", "candidate.findings"])
def test_bundled_findings_name_the_distribution_that_must_move(record, observation):
    """A bundled copy is only fixed by moving the thing that bundles it.

    This is the specific trap the scan's own triage note called out, and the image proved it:
    top-level wheel was already patched to 0.46.3 while setuptools' vendored 0.45.1 stayed.
    Recording only "wheel 0.45.1" would invite the upgrade that does nothing, so every bundled
    entry must name its containing distribution and that distribution's version.
    """
    entries = (
        record["findings"]
        if observation == "findings"
        else record["candidate"]["findings"]
    )
    bundled = [entry for entry in entries if entry["bundled"]]
    assert bundled, (
        "at least four of the five findings are bundled copies; none are recorded"
    )
    for entry in bundled:
        container = entry.get("bundled_in") or {}
        assert container.get("name"), (
            f"{entry['advisory']} is a bundled copy of {entry['package']}, but the record does "
            "not name the distribution that bundles it — that is the package a fix must move"
        )
        assert container.get("version"), (
            f"{entry['advisory']}: {container['name']} needs its observed version, otherwise a "
            "re-pin cannot tell whether the containing distribution actually moved"
        )
        assert entry["location"].startswith("/"), (
            f"{entry['advisory']}: an absolute in-image path is what distinguishes the vendored "
            f"copy from the top-level one, got {entry['location']!r}"
        )


@pytest.mark.parametrize("observation", ["findings", "candidate.findings"])
def test_setuptools_vendored_findings_state_the_version_that_fixes_them(
    record, observation
):
    """The candidate must move setuptools to the version that fixes its private copies."""
    entries = (
        record["findings"]
        if observation == "findings"
        else record["candidate"]["findings"]
    )
    vendored_by_setuptools = [
        entry
        for entry in entries
        if (entry.get("bundled_in") or {}).get("name") == "setuptools"
    ]
    assert len(vendored_by_setuptools) == 2, (
        "the scan attributed two setuptools-vendored findings (jaraco-context, wheel) to this "
        f"image; the record has {len(vendored_by_setuptools)}"
    )
    for entry in vendored_by_setuptools:
        threshold = entry.get("fixed_by_containing_version")
        assert threshold, (
            f"{entry['advisory']}: record the setuptools version that vendors a fixed copy; "
            "the advisory's own fix version names a package this image never installs directly"
        )
        if observation == "findings":
            assert entry["bundled_in"]["version"] != threshold
        else:
            assert entry["bundled_in"]["version"] == threshold
            assert entry["status"] == "fixed"
            assert entry["scanner_match"] is False


@pytest.mark.parametrize("observation", ["findings", "candidate.findings"])
def test_unfixed_findings_carry_a_disposition_reason(record, observation):
    """Anything left open must say why, in specific terms.

    An unresolved high finding with no recorded reason is indistinguishable from one nobody
    looked at. This keeps the "why it is acceptable for now" argument beside the finding rather
    than only in prose a future reader may not find.
    """
    entries = (
        record["findings"]
        if observation == "findings"
        else record["candidate"]["findings"]
    )
    for entry in entries:
        if entry["status"] == "open":
            reason = entry.get("reason", "")
            assert len(reason) > 40, (
                f"{entry['advisory']} is open but its reason is too thin to act on: {reason!r}"
            )
