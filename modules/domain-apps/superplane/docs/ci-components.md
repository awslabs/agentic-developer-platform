# Superplane component CI

`Superplane Domain CI` emits the stable required check `Superplane domain tests`
on every pull request to `main`. The classifier in
`.github/scripts/superplane_ci_scope.py` selects independent jobs from the changed
paths. Mixed changes select the union of their consumers.

| Changed component | Checks selected |
| --- | --- |
| `src/superplane-api/` | API tests and coverage gates |
| `src/superplane-controller/` | Controller Go tests and execution integration tests |
| `src/superplane-platform-monitor/` | Monitor Go tests and observation coverage |
| `ui/` or shared gateway frontend | Superplane UI tests, lint, typecheck and fixture browser checks |
| Shared worker image or security bundle | Worker packaging, agent tests and pinned SkyPilot compatibility |
| Superplane agent assets | Worker checks and persona registration |
| Shared deployment or teardown scripts | Offline lifecycle and registration integration |
| Gateway auth, shared code or domain proxy | Superplane gateway integration and authorization checks |
| Superplane auth | Domain, API and gateway checks |
| Harness jobs or executor | Domain, API, controller and worker checks |
| Shared contracts, release inputs or unknown `src/` components | All components |
| Other Superplane module paths | Domain tests, lint and domain coverage gates |
| Unrelated platform changes | Classifier checks only |

The classifier contains the exact shared dependency mapping. Update it and its
regression tests when adding a component or a shared dependency. Shared packaging
and installation inputs select their consumer jobs conservatively. Changes to the
classifier or its workflow run all components; normal manual runs do likewise.
The existing controller-only manual diagnostic remains separate from the required gate.

Browser checks are called by Superplane CI, rather than unconditionally by Gateway
CI. Gateway CI retains its own integrated frontend build and tests.

The required check rejects failures, cancellations and unexpected skips in any
selected job. Renames and deletions select the consumers of the old paths too.
Unselected jobs do not imply test coverage. These checks are offline and do not
install Superplane into an AWS environment or require it to be deployed.
