"""R10 acceptance 1 — shared reasoning sessions and isolated paid execution.

Issue #5050 (U5), EPIC #4910.

The requirement makes this a *choice with a stated reason*: Agent Factory hosting is the
default, and a dedicated Superplane queue/ScaledJob needs a genuinely different isolation,
concurrency, IAM or image requirement. These tests permit only the reviewed paid
executor and require its credential isolation and hosting controls. A new lane still
fails until its separate justification and boundary checks are reviewed.

The suite is conditional in the same way the requirement is.
`test_any_added_scaledjob_carries_the_required_knobs` requires a justified lane to carry
`failedJobsHistoryLimit: 5` and `karpenter.sh/do-not-disrupt: "true"` — so the tests do not
retain the operational protections of the precedent.

Why "a second hosting path" is the thing being prevented: a divergent lane inherits none of
Agent Factory's fixes. That is the blast radius the story names, and it is silent — the
second lane keeps working while falling behind.
"""

from __future__ import annotations

from pathlib import Path

import yaml

pytest_plugins = ("_hosting_fixtures",)

# tests/[0] agent/[1] superplane/[2] domain-apps/[3] modules/[4] root/[5]
_REPO_ROOT = Path(__file__).resolve().parents[5]
_SUPERPLANE = _REPO_ROOT / "modules" / "domain-apps" / "superplane"
_HOSTING_DIR = Path(__file__).resolve().parent.parent / "hosting"

_AGENT_FACTORY_SCALEDJOB = (
    _REPO_ROOT
    / "modules"
    / "agent-factory"
    / "webhook-ingress"
    / "infra"
    / "scaledjob.tf"
)


def _superplane_scaledjob_docs() -> list[tuple[Path, dict]]:
    """Every ScaledJob manifest under the Superplane module.

    Parsed rather than grepped so a `kind: ScaledJob` inside a comment or a string does not
    register as a manifest, and so the knob assertions can read real structure.
    """
    found: list[tuple[Path, dict]] = []
    for path in _SUPERPLANE.rglob("*.y*ml"):
        if "node_modules" in path.parts:
            continue
        try:
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        for document in documents:
            if isinstance(document, dict) and document.get("kind") == "ScaledJob":
                found.append((path, document))
    return found


class TestReasoningSessionsReuseAgentFactory:
    """Reasoning uses the shared lane; only the reviewed paid executor is distinct."""

    def test_only_the_explicitly_isolated_paid_executor_has_a_scaledjob(self):
        found = _superplane_scaledjob_docs()
        expected = _SUPERPLANE / "executor/deploy/paid-worker.yaml"
        assert [path for path, _ in found] == [expected], (
            "Any additional lane needs its own explicit R10 isolation/IAM/image review."
        )
        document = found[0][1]
        assert document["metadata"]["name"] == "superplane-paid-worker"
        job = document["spec"]["jobTargetRef"]
        assert job["backoffLimit"] == 0
        assert job["parallelism"] == job["completions"] == 1
        pod = job["template"]["spec"]
        assert pod["serviceAccountName"] == "superplane-paid-worker"
        (trusted,) = pod["containers"]
        (controller,) = pod["initContainers"]
        assert trusted["name"] == "paid-worker"
        assert controller["name"] == "scoped-controller"
        assert {mount["name"] for mount in controller["volumeMounts"]} == {"task"}
        assert "database" in {mount["name"] for mount in trusted["volumeMounts"]}
        readme = (_HOSTING_DIR / "README.md").read_text(encoding="utf-8")
        assert "superplane-paid-worker" in readme and "paid_domain_operation" in readme

    def test_the_hosting_module_adds_no_terraform(self):
        """No lane means no Terraform, which is why rollback is just a revert.

        Stated in the story's Deployment section: infrastructure a revert does not remove is
        the trap this avoids.
        """
        terraform = list(_HOSTING_DIR.rglob("*.tf"))

        assert terraform == [], (
            f"The hosting module introduced Terraform: {[p.name for p in terraform]}. "
            "A dedicated lane requires a Terraform apply dispatch and separate destroy on rollback."
        )

    def test_no_superplane_specific_agent_queue_was_added(self):
        """Agent inboxes are not workload queues (acceptance 4's carried-forward boundary).

        A Superplane-specific SQS queue for reasoning sessions would be both an unjustified
        second hosting path and a step into B's workload lifecycle.
        """
        offenders: list[str] = []
        for path in _SUPERPLANE.rglob("*.tf"):
            source = path.read_text(encoding="utf-8")
            if 'resource "aws_sqs_queue"' in source:
                offenders.append(str(path.relative_to(_REPO_ROOT)))

        assert offenders == [], (
            f"An SQS queue was added under the Superplane module: {offenders}. "
            "Reasoning sessions use Agent Factory's existing agent-submit queue."
        )


class TestTheDecisionIsRecordedWithItsReason:
    """A choice R10 requires to be argued must be findable, not just implied by absence."""

    def test_the_readme_states_the_agent_factory_choice(self):
        readme = (_HOSTING_DIR / "README.md").read_text(encoding="utf-8")

        assert "Agent Factory" in readme
        assert "no dedicated lane" in readme.lower()

    def test_the_readme_rejects_the_upstream_shape_as_a_reason(self):
        """The specific non-reason R10 calls out.

        Pinned because it is the argument most likely to be made later by someone reading
        the upstream repo rather than the requirement.
        """
        readme = (_HOSTING_DIR / "README.md").read_text(encoding="utf-8")

        assert "upstream deployment shape is explicitly not a reason" in readme.lower()

    def test_the_readme_addresses_all_four_dimensions(self):
        """Isolation, concurrency, IAM and image — the four R10 names.

        A decision that skipped one would be an incomplete justification for reuse.
        """
        readme = (_HOSTING_DIR / "README.md").read_text(encoding="utf-8").lower()

        for dimension in ("isolation", "concurrency", "iam", "image"):
            assert f"**{dimension}**" in readme, (
                f"The hosting decision does not address {dimension}."
            )


class TestTheReusedLaneStillProvidesWhatAcceptance1Requires:
    """The reuse argument depends on Agent Factory's lane having these properties.

    If that lane loses them, the justification for reusing it stops holding — so this reads
    the real file rather than trusting the README's table.
    """

    def test_agent_factory_lane_scales_to_zero_and_keeps_failure_evidence(self):
        source = _AGENT_FACTORY_SCALEDJOB.read_text(encoding="utf-8")

        assert "minReplicaCount: 0" in source, (
            "The reused lane no longer scales to zero."
        )
        assert "failedJobsHistoryLimit: 5" in source, (
            "The reused lane no longer keeps 5 failed jobs; acceptance 1 names that value as failure evidence."
        )

    def test_agent_factory_lane_protects_running_pods_from_node_reclaim(self):
        source = _AGENT_FACTORY_SCALEDJOB.read_text(encoding="utf-8")

        assert 'karpenter.sh/do-not-disrupt: "true"' in source, (
            "The reused lane no longer sets do-not-disrupt; a mid-run reasoning session could be reclaimed."
        )

    def test_superplane_personas_reach_the_shared_agent_image(self):
        """The image dimension of the reuse argument, checked at its source.

        The two Superplane personas are registered in Agent Factory's catalogue and stage
        into the same `adp-agent-runtime` image, which is why "no different image
        requirement" is true rather than assumed.
        """
        personas = (
            _REPO_ROOT
            / "modules"
            / "agent-factory"
            / "webhook-ingress"
            / "lambda"
            / "common"
            / "personas.py"
        ).read_text(encoding="utf-8")

        assert "@agent-superplane-operator" in personas
        assert "@agent-superplane-researcher" in personas

        dockerfile = (
            _REPO_ROOT
            / "modules"
            / "agent-factory"
            / "agent-worker-image"
            / "Dockerfile"
        ).read_text(encoding="utf-8")

        assert "COPY modules/domain-apps/ /source/domain-apps/" in dockerfile, (
            "The shared image no longer copies domain-app assets, so the 'same image' reuse argument fails."
        )


class TestAnyAddedLaneMustFollowThePrecedent:
    """Conditional, exactly as R10 is.

    The paid executor is explicitly justified. Any such lane must keep the two
    knobs acceptance 1 names — the failure
    modes are a lane that discards failure evidence, and a lane whose pods get reclaimed
    mid-session.
    """

    def test_any_added_scaledjob_carries_the_required_knobs(self):
        for path, document in _superplane_scaledjob_docs():
            spec = document.get("spec", {})
            location = path.relative_to(_REPO_ROOT)

            assert spec.get("failedJobsHistoryLimit") == 5, (
                f"{location}: acceptance 1 requires failedJobsHistoryLimit: 5 for failure evidence."
            )
            assert spec.get("minReplicaCount") == 0, (
                f"{location}: acceptance 1 requires scale-to-zero."
            )
            # A ScaledJob creates finite Jobs. ScaledObject-only HPA controls are
            # not a substitute for its queue trigger and bounded Job lifetime.
            assert "scaleTargetRef" not in spec and "cooldownPeriod" not in spec
            assert spec["jobTargetRef"]["activeDeadlineSeconds"] > 0
            assert any(
                trigger["type"] == "aws-sqs-queue" for trigger in spec["triggers"]
            )

            annotations = (
                spec.get("jobTargetRef", {})
                .get("template", {})
                .get("metadata", {})
                .get("annotations", {})
            )
            assert annotations.get("karpenter.sh/do-not-disrupt") == "true", (
                f'{location}: acceptance 1 requires karpenter.sh/do-not-disrupt: "true" '
                "against mid-run node reclaim."
            )
