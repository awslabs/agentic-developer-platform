"""The renewer can mint only the reader token; reader RBAC stays read-only."""

from types import SimpleNamespace

from superplane_bootstrap.grant_plan import BootstrapRelease, compile_grants


def test_native_issuer_grants_are_exact_and_retained_only_on_success():
    journal = SimpleNamespace(
        target=SimpleNamespace(
            account_id="123456789012",
            workspace_id="22222222-2222-4222-8222-222222222222",
            org_id="11111111-1111-4111-8111-111111111111",
            cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/workspace",
        ),
        generation="a" * 64,
        original_allocation_id=None,
    )
    release = BootstrapRelease(
        "superplane-system",
        "superplane-controller",
        "superplane-controller",
        "v1.35",
        ("nodepools.superplane.ai",),
        reader_renewal=True,
    )
    principals = {
        actor: "arn:aws:iam::123456789012:role/" + actor
        for actor in ("registrar", "installer", "supervisor")
    }
    grants = compile_grants(journal, release, principals)["grants"]
    reader = [g for g in grants if g["key"].startswith("reader-issuer-")]
    assert len(reader) == 4
    assert all(
        g["lifetime"] == "workspace" and g["actor"] == "registrar" for g in reader
    )
    role = next(g["body"] for g in reader if g["key"] == "reader-issuer-namespace-role")
    assert role["metadata"]["namespace"] == release.namespace
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["serviceaccounts"],
            "verbs": ["get"],
            "resourceNames": ["superplane-controller"],
        },
        {
            "apiGroups": [""],
            "resources": ["serviceaccounts/token"],
            "verbs": ["create"],
            "resourceNames": ["superplane-controller"],
        },
    ]
    for grant in reader:
        if grant["body"]["kind"].endswith("Binding"):
            # EKS rejects a group longer than 63 characters before bootstrap.
            assert len(grant["body"]["subjects"][0]["name"]) <= 63
            assert grant["body"]["subjects"] == [
                {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Group",
                    "name": "superplane:reader:" + journal.target.workspace_id,
                }
            ]
