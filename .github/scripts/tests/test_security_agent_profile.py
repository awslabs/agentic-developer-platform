"""Gate for the validated AWS Security Agent profile (U0, issue #4439).

`.github/security/security-agent-profile.json` is an interface: U4-U8 assert
against it instead of retyping literals out of prose. These tests are the
mechanical gate listed in #4439's Validation section.

Every assertion here exists because a wrong value has a named blast radius:
  - maxTaskHours > 2      -> the ceiling protecting dev is wrong at the source
  - provisioning not enum -> wave-2 sequencing is undetermined (U13 unknown)
  - exclusion w/o cost    -> coverage silently reduced while appearing safe
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = REPO_ROOT / ".github" / "security" / "security-agent-profile.json"
SCHEMA_PATH = REPO_ROOT / ".github" / "security" / "security-agent-profile.schema.json"
COMPANION_DOC = REPO_ROOT / "docs" / "security" / "security-agent-pentest-validated.md"
RUNBOOK = REPO_ROOT / "docs" / "security" / "security-agent-runbook.md"

VALID_PROVISIONING = {"present", "self_upgradable", "image_rebuild_required"}

# First-run bound, decision D-18. The runbook's worked example ships 20.
MAX_TASK_HOURS_CEILING = 2


@pytest.fixture(scope="module")
def profile():
    """The profile, parsed. Fails loudly if absent or malformed."""
    assert PROFILE_PATH.is_file(), f"profile artifact missing: {PROFILE_PATH}"
    with PROFILE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def schema():
    assert SCHEMA_PATH.is_file(), f"schema missing: {SCHEMA_PATH}"
    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- schema


def test_profile_is_valid_json(profile):
    """The artifact parses as a JSON object."""
    assert isinstance(profile, dict)


def test_profile_validates_against_schema(profile, schema):
    """Profile JSON validates against its schema.

    Skipped rather than failed when jsonschema is unavailable: the
    field-level tests below independently cover every gate #4439 names, so a
    missing optional dependency must not turn the whole gate green-by-absence
    nor red for an unrelated reason.
    """
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema not installed")
    jsonschema.validate(instance=profile, schema=schema)


# ---------------------------------------------------------------- SP-1 runtime


def test_runtime_provisioning_is_a_valid_enum_value(profile):
    """runtime.provisioning is present and exactly one of the three values.

    This is the plan's branch selector: `image_rebuild_required` means U13 must
    exist and blocks U4-U7. A prose answer or a null cannot be asserted against,
    so anything outside the enum fails the build.
    """
    provisioning = profile["runtime"]["provisioning"]
    assert provisioning is not None, "runtime.provisioning must not be null"
    assert provisioning in VALID_PROVISIONING, (
        f"runtime.provisioning={provisioning!r} is not one of {sorted(VALID_PROVISIONING)}"
    )


def test_self_upgradable_carries_an_install_command(profile):
    """If provisioning is self_upgradable, U4 needs the command that fixes it."""
    runtime = profile["runtime"]
    if runtime["provisioning"] != "self_upgradable":
        pytest.skip("provisioning is not self_upgradable")
    install_command = runtime.get("install_command") or ""
    assert install_command.strip(), (
        "self_upgradable requires a non-empty runtime.install_command — "
        "U4 owns the in-session fix and needs the exact command"
    )


def test_image_rebuild_required_names_the_blocking_unit(profile):
    """The branch that changes the plan's shape must say so explicitly."""
    runtime = profile["runtime"]
    if runtime["provisioning"] != "image_rebuild_required":
        pytest.skip("provisioning is not image_rebuild_required")
    blocking = json.dumps(runtime).lower()
    assert "u13" in blocking, (
        "image_rebuild_required must reference U13, the unit that blocks U4-U7"
    )


def test_minimum_client_versions_are_present(profile):
    """min_cli_version and min_botocore_version are present and non-empty."""
    runtime = profile["runtime"]
    for key in ("min_cli_version", "min_botocore_version"):
        value = runtime.get(key)
        assert isinstance(value, str) and value.strip(), (
            f"runtime.{key} must be a non-empty string"
        )


def test_minimum_versions_look_like_versions(profile):
    """A version field holding prose is unusable for a preflight comparison."""
    runtime = profile["runtime"]
    for key in ("min_cli_version", "min_botocore_version"):
        assert re.fullmatch(r"\d+(\.\d+)*", runtime[key].strip()), (
            f"runtime.{key}={runtime[key]!r} is not a dotted numeric version"
        )


def test_observed_versions_are_recorded(profile):
    """SP-1 requires the exact observed versions, not just the decision."""
    observed = profile["runtime"]["observed"]
    for key in ("aws_cli", "botocore", "python"):
        assert str(observed.get(key, "")).strip(), (
            f"runtime.observed.{key} must be recorded"
        )


def test_preflight_assertion_is_copyable(profile):
    """U4 copies this verbatim; an empty string would silently gate nothing."""
    assertion = profile["runtime"].get("preflight_assertion", "")
    assert assertion.strip(), "runtime.preflight_assertion must be non-empty"
    assert "securityagent" in assertion, (
        "the preflight assertion must actually probe for the securityagent service"
    )


def test_observed_botocore_meets_the_stated_minimum(profile):
    """Internal consistency: what we observed must satisfy what we require."""

    def parts(version):
        return tuple(int(p) for p in version.strip().split("."))

    runtime = profile["runtime"]
    assert parts(runtime["observed"]["botocore"]) >= parts(
        runtime["min_botocore_version"]
    ), "observed botocore is below the profile's own stated minimum"


# ---------------------------------------------------------------- SP-4 pentest


def test_pentest_max_task_hours_is_bounded(profile):
    """pentest.maxTaskHours is present and <= 2 (first-run bound, D-18)."""
    max_task_hours = profile["pentest"]["maxTaskHours"]
    assert isinstance(max_task_hours, (int, float)) and not isinstance(
        max_task_hours, bool
    )
    assert 0 < max_task_hours <= MAX_TASK_HOURS_CEILING, (
        f"maxTaskHours={max_task_hours} must be >0 and <= {MAX_TASK_HOURS_CEILING}; "
        "the runbook's 20-hour example is the anti-pattern this unit replaces"
    )


def test_request_shape_max_task_hours_matches_the_profile(profile):
    """The shape we will actually send must carry the same bound.

    A bound recorded at the top level but not in the request body would be a
    ceiling that protects nothing.
    """
    pentest = profile["pentest"]
    assert pentest["request_shape"]["maxTaskHours"] == pentest["maxTaskHours"], (
        "request_shape.maxTaskHours disagrees with pentest.maxTaskHours"
    )


def test_request_shape_does_not_reuse_the_unsafe_example(profile):
    """N-2: never silently adopt excludeRiskTypes: [] + maxTaskHours: 20."""
    shape = profile["pentest"]["request_shape"]
    assert shape["maxTaskHours"] != 20, (
        "maxTaskHours 20 is the forbidden runbook example"
    )
    assert shape["excludeRiskTypes"], (
        "an empty excludeRiskTypes is the runbook's 'test everything' anti-pattern"
    )


def test_every_exclusion_states_its_coverage_cost(profile):
    """An exclusion with no stated cost fails the build.

    This is the gate that stops us silently reducing pentest coverage while
    appearing to comply with a safety requirement.
    """
    exclusions = profile["pentest"]["excludeRiskTypes"]
    assert exclusions, "expected at least one recorded exclusion"
    for entry in exclusions:
        value = entry.get("value", "<unnamed>")
        cost = entry.get("coverage_cost")
        assert isinstance(cost, str) and cost.strip(), (
            f"exclusion {value!r} has no non-empty coverage_cost"
        )


def test_exclusions_are_classified(profile):
    """Each exclusion says whether it is a destructive action or a vuln class.

    N-3: excluding vulnerability classes is not a safety control. Forcing the
    classification makes an attempt to do so visible in review.
    """
    for entry in profile["pentest"]["excludeRiskTypes"]:
        assert entry.get("classification") in {
            "destructive_action",
            "vulnerability_class",
        }, f"exclusion {entry.get('value')!r} is missing a valid classification"


def test_coverage_defining_risk_types_are_not_excluded(profile):
    """N-3: the pentest must still hunt what the actor matrix exists to find."""
    excluded = {e["value"] for e in profile["pentest"]["excludeRiskTypes"]}
    must_remain = {"PRIVILEGE_ESCALATION", "INSECURE_DIRECT_OBJECT_REFERENCE"}
    leaked = excluded & must_remain
    assert not leaked, (
        f"{sorted(leaked)} must not be excluded — these are exactly the classes the "
        "four-actor matrix exists to detect; excluding them deletes the pentest's point"
    )


def test_request_shape_excludes_match_the_documented_exclusions(profile):
    """The justified list and the list we send must be the same set."""
    pentest = profile["pentest"]
    documented = {e["value"] for e in pentest["excludeRiskTypes"]}
    sent = set(pentest["request_shape"]["excludeRiskTypes"])
    assert documented == sent, (
        f"request_shape excludes {sorted(sent)} but only {sorted(documented)} carry a "
        "documented coverage cost"
    )


def test_request_shape_title_is_charset_legal(profile):
    """title accepts only letters, digits, hyphen, underscore; <=100 chars."""
    title = profile["pentest"]["request_shape"]["title"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,100}", title), (
        f"title={title!r} violates the API charset (no spaces or colons)"
    )


def test_pentest_never_opens_remediation_prs(profile):
    """A nightly pentest must not open fix PRs against this repo."""
    assert (
        profile["pentest"]["request_shape"].get("codeRemediationStrategy") == "DISABLED"
    )


def test_destructiveness_answer_is_explicit(profile):
    """SP-4's real question gets a boolean plus a stated finding."""
    pentest = profile["pentest"]
    assert isinstance(pentest["destructiveness_axis_exists"], bool)
    assert pentest["destructiveness_finding"].strip(), (
        "SP-4 must state plainly whether a destructiveness axis exists"
    )


def test_sp4_acceptance_questions_are_answered(profile):
    """A reviewer must be able to answer both SP-4 questions from the profile."""
    pentest = profile["pentest"]
    for key in ("what_this_job_will_try_to_do", "what_this_job_will_never_find"):
        assert pentest[key].strip(), f"pentest.{key} must be answered"


# ---------------------------------------------------------------- SP-2 findings


def test_finding_id_stability_is_a_boolean(profile):
    """finding_id_stable is a boolean, not null.

    U8's dedup strategy forks on this: stable IDs mean dedup keys on them,
    unstable IDs mean content fingerprinting (materially more work).
    """
    findings = profile["findings"]
    assert isinstance(findings["finding_id_stable"], bool), (
        "findings.finding_id_stable must be a boolean, not null or prose"
    )
    assert findings["finding_id_stability_evidence"].strip(), (
        "the stability answer must carry the evidence that establishes it"
    )


def test_unstable_ids_carry_a_dedup_implication(profile):
    """If IDs are unstable, U8 must be told what to key on instead."""
    findings = profile["findings"]
    if findings["finding_id_stable"]:
        pytest.skip("finding IDs reported stable")
    assert findings["dedup_implication"].strip(), (
        "unstable IDs require an explicit dedup_implication for U8"
    )


def test_findings_list_response_key_is_recorded(profile):
    """The runbook's queries assume `findings`; the API returns summaries.

    Recording the real key is what stops U8 silently parsing null.
    """
    assert profile["findings"]["list_response_key"].strip()


def test_findings_schema_fields_include_dedup_signals(profile):
    """U8 needs the fields it will fingerprint on to be documented."""
    fields = set(profile["findings"]["schema_fields"])
    for required in ("findingId", "name", "riskType", "codeLocations"):
        assert required in fields, f"findings.schema_fields is missing {required!r}"


# ---------------------------------------------------------------- SP-5 / SP-6


def test_target_verification_method_is_decided(profile):
    """SP-5: a method is chosen, justified, and has a named bootstrap owner."""
    target = profile["target_domain"]
    assert target["verification_method"] in {"DNS_TXT", "HTTP_ROUTE", "PRIVATE_VPC"}
    for key in (
        "chosen_because",
        "verification_artifact",
        "bootstrap_owner",
        "durability",
    ):
        assert target[key].strip(), f"target_domain.{key} must be answered"


def test_actor_matrix_is_not_tbd(profile):
    """N-4: either the matrix is buildable, or the reduction is stated."""
    actors = profile["actor_matrix"]
    assert actors["decision"] in {
        "SECRETS_MANAGER",
        "AWS_LAMBDA",
        "AWS_IAM_ROLE",
        "AWS_INTERNAL",
    }
    assert actors["decision_rationale"].strip()
    assert actors["net_new_component_scope"].strip()
    assert isinstance(actors["four_distinct_identities_in_two_orgs_exist"], bool)


def test_incomplete_actor_matrix_states_its_coverage_reduction(profile):
    """A reduced matrix must name what coverage it gives up."""
    actors = profile["actor_matrix"]
    if actors["four_distinct_identities_in_two_orgs_exist"]:
        pytest.skip("full four-actor matrix available")
    assert actors.get("coverage_reduction_accepted", "").strip(), (
        "a reduced actor matrix must state the coverage it knowingly sacrifices (N-4)"
    )


def test_actors_in_request_shape_all_exist_today(profile):
    """We must not ship a request referencing identities that do not exist."""
    profile_actors = {
        a["identifier"] for a in profile["pentest"]["request_shape"]["assets"]["actors"]
    }
    existing = set(profile["actor_matrix"]["identities_that_exist_today"])
    missing = profile_actors - existing
    assert not missing, (
        f"request_shape references non-existent identities: {sorted(missing)}"
    )


# ---------------------------------------------------------------- docs / S-1


def test_companion_doc_exists_with_a_drift_table(profile):
    """Structural doc assertion: the drift table has at least one row."""
    assert COMPANION_DOC.is_file(), f"companion doc missing: {COMPANION_DOC}"
    rows = [
        line
        for line in COMPANION_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| ")
    ]
    assert rows, "the companion doc must contain at least one table row"


def test_runbook_is_not_deleted(profile):
    """Regression check: corrections are additive, never a rewrite."""
    assert RUNBOOK.is_file(), "the original runbook must not be deleted"
    assert RUNBOOK.read_text(encoding="utf-8").strip(), (
        "the runbook must not be emptied"
    )


def test_every_downstream_verb_appears_in_the_companion_doc(profile):
    """Every service verb in the code-review and pentest call lists is documented."""
    doc = COMPANION_DOC.read_text(encoding="utf-8")
    verbs = set(profile["code_review"]["verbs"]) | set(profile["pentest"]["verbs"])
    missing = sorted(v for v in verbs if v not in doc)
    assert not missing, f"verbs absent from the companion doc: {missing}"


def test_profile_ships_no_secret_material(profile):
    """S-5: no tokens or credential values in the artifact.

    Secret ARNs are references, not secrets, and are allowed. Actual token
    material is not.
    """
    blob = json.dumps(profile)
    for marker in ("-----BEGIN", "ghp_", "github_pat_", "AKIA", "ASIA"):
        assert marker not in blob, (
            f"profile appears to contain secret material: {marker}"
        )
