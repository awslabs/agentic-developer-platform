# Publishing documentation

ADP's documentation is public. Never commit real deployment account IDs,
environment URLs, operator profiles, network resource IDs, Cognito pool IDs,
customer identities, credentials, or private operational evidence.

Use reserved `example.com`, `example.org`, `example.net`, `.example`, `.test`,
or `.invalid` domains. Account numbers beginning `000000000` and familiar AWS
example accounts such as `123456789012` are illustrative. Resource examples use
zero-filled identifiers. Replace examples using private operator configuration;
they are not approved targets and must not be copied into a live deployment.

Keep real target configuration in protected GitHub environment secrets or an
access-controlled operator store. Runtime configuration must not depend on
publishing live identities in `/docs`. CLI regression setup is documented in
[the private target configuration guide](regression-testing/cli-uplift-evaluation.md#private-target-configuration).

Historical reports and JSON evidence under `/docs` have had deployment identities
sanitized. Their results and code references provide context, but these public
copies are not exact operational provenance. Consult private records before
using a policy, deployment receipt, or recovery command. Public source links and
official vendor documentation may remain.

Before publishing documentation, run `python3 scripts/check-public-docs.py` and
review the content for identities the pattern check cannot recognize. Avoid
screenshots or raw logs containing private deployment details. The check reports
file locations and finding categories without printing matched values.
