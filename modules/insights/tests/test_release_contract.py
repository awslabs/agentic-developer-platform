"""Verify release evidence is bound to one checked deployment attempt."""

import importlib.util
import pathlib
import unittest


MODULE = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = pathlib.Path(__file__).resolve().parents[3] / ".github" / "workflows" / "insights-deploy.yml"
spec = importlib.util.spec_from_file_location("insights_release_evidence", MODULE / "scripts" / "release_evidence.py")
evidence_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence_module)
ACCOUNT = "000000000000"
REGION = "us-east-1"
CLUSTER = "adp-dev-eks-cluster"
ENV = {
    "ADP_SOURCE_REVISION": "a" * 40,
    "ADP_WORKFLOW_REVISION": "b" * 40,
    "ADP_ACCOUNT_ID": ACCOUNT,
    "ADP_ENVIRONMENT": "dev",
    "AWS_REGION": REGION,
    "GITHUB_REPOSITORY_ID": "10",
    "GITHUB_RUN_ID": "20",
    "GITHUB_RUN_ATTEMPT": "1",
    "ADP_INPUT_ENVIRONMENT": "dev",
    "ADP_INPUT_STAGE": "infra",
    "ADP_INPUT_MODE": "apply",
    "ADP_INPUT_IMAGE": "",
    "ADP_CORRELATION": "engine-1",
}


def read(parts):
    if parts[0] == "sts":
        return {"Account": ACCOUNT}
    return {"cluster": {"name": CLUSTER, "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}"}}


class ReleaseContractTest(unittest.TestCase):
    def test_context_and_release_bind_same_run_and_actual_module_files(self):
        context, release = evidence_module.evidence(ENV, read)
        self.assertEqual(context["resource_id"], CLUSTER + "/adp-insights")
        self.assertEqual(context["inputs"], {"environment": "dev", "stage": "infra", "mode": "apply", "image": ""})
        self.assertEqual(context["correlation"], "engine-1")
        self.assertEqual(release["run_id"], context["run_id"])
        self.assertEqual(release["workflow_revision"], context["workflow_revision"])
        self.assertIn("platform/workloads.tf", release["assets"])
        self.assertIsNone(release["image_digest"])

    def test_mismatched_account_or_cluster_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            evidence_module.evidence(ENV | {"ADP_ACCOUNT_ID": "111111111111"}, read)
        with self.assertRaisesRegex(ValueError, "cluster"):
            evidence_module.evidence(ENV, lambda parts: {"Account": ACCOUNT} if parts[0] == "sts" else
                                     {"cluster": {"name": CLUSTER, "arn": "wrong"}})

    def test_workflow_has_scoped_dispatch_and_run_bound_artifacts(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("branches: [main]", workflow)
        self.assertIn("- 'modules/insights/**'", workflow)
        self.assertIn("options: [dev, prod]", workflow)
        self.assertIn("options: [infra, emitter, distiller, verify]", workflow)
        self.assertIn('git show "$ADP_WORKFLOW_REVISION:.github/workflows/insights-deploy.yml"', workflow)
        self.assertIn("adp-deployment-context-${{ github.run_attempt }}", workflow)
        self.assertIn("adp-release-insights-${{ github.run_attempt }}", workflow)
        self.assertIn("if: env.ADP_MODE == 'apply'", workflow)
        self.assertIn("uses: ./.github/actions/trusted-deployment", workflow)
        self.assertIn("TF_VAR_deployment_role_boundaries: ${{ vars.ADP_INSIGHTS_ROLE_BOUNDARIES || '{}' }}", workflow)


if __name__ == "__main__":
    unittest.main()
