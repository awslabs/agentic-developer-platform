# Native API test-profile preparation

New release-verification inputs require `test_profile_ref`, with record type
`test_profile`, the selected AWS connection, native Asset ID and a positive exact
version. The owner-selected manual trigger must pin the same reference. Saving a
selector does not qualify or enable release verification.

The read-only `NativeTestProfileReader` prepares a digest receipt using the bound
provider session. It verifies the exact space, Asset identity/version, complete
metadata and `test_agent_type=releaseApiTesting`. It bounds generic Asset file
pagination and content, rejects omitted/truncated observations, and revalidates
current authority before and after each read. Its receipt contains digests and
references, not profile contents or credentials. It remains unmounted.

Generic Asset transport does not establish the test-profile content schema.
The receipt always reports `unqualified`. New admission and native submission
remain blocked by these concrete gaps:

- `test_profile_schema_unqualified`: qualify the effective API specification,
  credential/persona and network configuration from an authoritative schema.
- `test_profile_version_unqualified`: establish which exact profile version the
  ID-only native trigger consumes. Repeated Asset reads do not prove this.
- `verification_api_target_unavailable`: select an independently verified HTTP
  API deployment. The queue fixture's security group and Lambda execution role
  are not an API URL or application credential.
- `native_report_schema_unqualified`: qualify per-scenario result semantics and
  original execution correlation before interpreting a native report.

The prepared `devops-release-verification-v2` description uses the documented
`testProfileId` and nonempty `webhookEventMessage` envelope. The message retains
the original run, scope, attempt, token, deployment/source/scenario references,
exact profile selector and bounded requirement. The complete canonical
description must fit the provider's 10,000-character limit. It cannot enforce
profile version or network bounds by itself.

Historical requests retain their exact original shape and description. An exact
receipt may replay only under current read authority; a changed request or a new
idempotency key cannot turn historical input into a new admission. Previously
accepted effects can reconcile, observe and collect with their original attempt
and token. Neither historical nor new inputs can create another native test
until qualification is implemented and reviewed. Existing independent deployment,
scenario outcome and owned-fixture cleanup checks continue to apply.
