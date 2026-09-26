# Nightly acceptance recovery — 26 September 2026

Scheduled run [36233688786](https://github.com/aws-e/adp/actions/runs/36233688786)
ran source `6b1a153e5aa6ad88935fba3bacde1b800986a7e5`.
The offline orchestration guards passed, but deployment snapshot job
`108381553085` failed with `lambda.get_function failed: AccessDeniedException`.
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
