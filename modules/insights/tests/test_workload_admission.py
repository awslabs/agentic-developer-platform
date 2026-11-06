"""Exercise the existing operator admission contract with Insights identities."""

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "insights_workload_inventory", ROOT / "platform/automation-infra/verify-workload-inventory.py"
)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)
ACCOUNT = "000000000000"
ROLES = {
    f"arn:aws:iam::{ACCOUNT}:role/adp-dev-insights-{service}":
    f"arn:aws:iam::{ACCOUNT}:policy/insights-{service}-ceiling"
    for service in ("emitter", "distiller")
}


class WorkloadAdmissionTest(unittest.TestCase):
    def verify(self, observed_boundaries):
        def read(*args):
            if args[:2] == ("sts", "get-caller-identity"):
                return {"Account": ACCOUNT}
            if args[:2] == ("iam", "get-role"):
                role = f"arn:aws:iam::{ACCOUNT}:role/{args[-1]}"
                boundary = observed_boundaries[role]
                return {"Role": {"Arn": role, "PermissionsBoundary":
                                 {"PermissionsBoundaryArn": boundary} if boundary else {}}}
            if args[:2] == ("iam", "get-policy"):
                return {"Policy": {"DefaultVersionId": "v1"}}
            if args[:2] == ("iam", "get-policy-version"):
                resource = "arn:aws:s3:::adp-dev-insights-evidence/assurance/v1/*/insights/*"
                return {"PolicyVersion": {"Document": {"Statement": [
                    {"Effect": "Allow", "Action": "s3:PutObject", "Resource": resource},
                    {"Effect": "Deny", "NotAction": "s3:PutObject", "Resource": "*"},
                    {"Effect": "Deny", "Action": "s3:PutObject", "NotResource": resource},
                ]}}}
            if args[:2] == ("iam", "list-roles"):
                return {"Roles": []}
            self.fail(f"Unexpected admission read: {args}")

        return inventory.verify_inventory({
            "account_id": ACCOUNT,
            "deployment_role_boundaries": ROLES,
            "deployment_execution_resources": [],
            "deployment_managed_policy_arns": [],
            "clusters": [],
        }, aws=read)

    def test_operator_admits_both_roles_with_their_approved_boundaries(self):
        result = self.verify(ROLES)
        self.assertEqual(result["roles"], 2)
        self.assertEqual(result["ceilings"], 2)

    def test_operator_rejects_missing_or_different_same_account_boundary(self):
        for role in ROLES:
            for boundary in (None, f"arn:aws:iam::{ACCOUNT}:policy/unreviewed-ceiling"):
                with self.subTest(role=role, boundary=boundary), self.assertRaisesRegex(
                    AssertionError, "Missing/wrong ceiling"
                ):
                    self.verify(ROLES | {role: boundary})


if __name__ == "__main__":
    unittest.main()
