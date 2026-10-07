# Portable trusted actions

Workflows that check out a selected scan revision load `trusted-scan`,
`trusted-checks`, and `trusted-rules` from the public
`awslabs/agentic-developer-platform` repository at a full commit SHA. This lets
both repositories resolve the same reviewed credential helpers without access
to a private upstream repository. Keep these helpers independent of the source
being scanned; replacing the remote reference with a local action would run the
credential helper from that selected source.

When updating a helper pin, first publish and review that helper revision in
the public repository. Verify its inputs and main-branch, event, role, and OIDC
checks, then update the references and trust-contract expectations in both
repositories. Keep workflow environments, permissions, and credential ordering
intact. Synchronizing source does not configure AWS roles or provision runners.

The `public-workflow-contracts` job runs on a GitHub-hosted runner without cloud
credentials. It rejects private upstream action dependencies and mutable public
credential-helper references on workflow, action, and contract-test changes.
Run its tests locally with:

```sh
python -m pytest -q platform/automation-infra/tests/test_workflow_portability.py
```
