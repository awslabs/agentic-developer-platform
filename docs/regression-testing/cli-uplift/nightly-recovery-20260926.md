# Nightly acceptance recovery — 26 September 2026

Scheduled run [36233688786](https://github.com/aws-e/adp/actions/runs/36233688786)
ran source `6b1a153e5aa6ad88935fba3bacde1b800986a7e5`.
The offline orchestration guards passed, but deployment snapshot job
`000000000210` failed with `lambda.get_function failed: AccessDeniedException`.
Onboarding, budget enforcement and EC2 were all skipped. The combined verdict
correctly failed; this run supplies no live acceptance or EC2 cleanup evidence.

The parent snapshot had no explicit evaluation credential step, and the EC2
child's revision snapshot preceded its existing OIDC credential step. The repair
uses the existing protected dev environment role secret before the parent read
and moves the child's existing credential guard/setup ahead of its revision read.
No IAM policy, runner role or fixture permissions are changed. Missing role
configuration still fails closed. Actual access with that role remains subject
to live verification; offline guards cannot prove AWS permissions.

Default manual dispatch also supplied the nonempty string `{}`, masking the
configured nightly fixtures. Empty input now uses the repository variable,
matching scheduled runs; explicit `{}` still intentionally selects no fixtures.
No additional schedule or paid scenario is introduced.

Epic #5644 remains incomplete. This failure does not supersede prior individual
EC2 acceptance records, and its repair does not close any story.

## Follow-up receipt qualification

PR #6417 merged as `2181bc48430ec0bcd40acfce74cb0ba5ced0e429` after all
four CI guards passed. Run 36266038172 passed explicit OIDC setup and parent
revision lookup. This establishes the authentication repair, not full nightly
acceptance.

The parent still loaded the bare example config, bypassing the dev gateway
binding used by the EC2 child. It now uses the reviewed dev binding and existing
Deployment/Service receipt verification, with no ambient target overrides.

The current gateway image is
`sha256:dc11fa26617ada3b62b768414950ec90694e732aa76b019e45de104d5e0e5378`,
source `a36f10a1a6898ef77c411fa615fe68de59f06b2d` (Task metadata contention repair).
CodeBuild `adp-dev-gateway-build:f9f8e9d3-32af-4e86-b3aa-88e3df005b1b` succeeded;
its log contains this source SHA and pushed image digest. Its S3 source archive
SHA-256 is `f2d6f05e07a029ce7d1e3425101a32242f0a9c16500b2288bcc11fc8978f6c9b`.
All 7,272 archived source files match the recorded commit byte for byte, with no
extra files. ECR maps the digest to the same full source SHA.

After adding this verified existing build receipt, a read-only check against the
live dev Deployment and Service passed and resolved the exact source SHA via
`gateway_eks_build_receipt`. This check used the existing devbox identity;
Actions OIDC verification of this follow-up remains separate. No gateway rollout
or runtime mutation was performed.
