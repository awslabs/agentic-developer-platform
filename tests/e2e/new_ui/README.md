# New UI shell acceptance — #5079 / #5112

This suite validates the deployed `/next` shell alongside the current UI. It does
not claim that any data page has migrated. Successful navigation and page-data
requests use the real deployment. No application records, users, AWS resources or
deployment flags are changed by these tests.

## Run against a verified deployment

Requires the corrected shared Cognito helpers from **PR #5115**, the shell and eager
fallback, and `/features` polling from **PR #5112**. Preview subscriptions schedule
a refresh every 15 seconds and on focus; browser throttling and network completion
can delay observation. The foreground simulation allows 35 seconds to observe exit.
Credentials remain in memory. The shared helper obtains real Cognito tokens and
restores the session through the SPA's normal startup path; this is not an OAuth
callback test. PR #5115 validates hosted OAuth separately.

```bash
pip install pytest playwright boto3
playwright install chromium

AWS_PROFILE=<confirmed-profile> \
E2E_NEW_UI_ENABLED=1 \
E2E_NEW_UI_EXPECTED_FLAG=on \
E2E_CLOUDFRONT_URL=https://<distribution>.cloudfront.net \
  python -m pytest tests/e2e/new_ui/ -v --junitxml=/tmp/new-ui-on.xml
```

After a separately authorized flag-off deployment, repeat with
`E2E_NEW_UI_EXPECTED_FLAG=off` and a distinct report. The suite never flips SSM or
deploys anything. `on` and `off` select their corresponding scenarios explicitly;
the opposite mode's marked tests are reported as deselected. Both deployment runs
are needed for complete coverage. A missing, failed or malformed `/api/features`
response, or a live flag different from the expected value, **fails setup**.

Without `E2E_NEW_UI_ENABLED=1`, only this package's tests skip. Missing dependencies
in an enabled run fail rather than skip. Skipped tests and deselected scenarios
provide **no acceptance evidence**. Collection and offline checks likewise do not
establish deployed behavior; record actual run results and deployed SHA separately.

| Variable | Purpose |
|---|---|
| `E2E_NEW_UI_ENABLED=1` | Explicitly opt into live tests |
| `E2E_NEW_UI_EXPECTED_FLAG=on\|off` | Required expected deployment state |
| `E2E_CLOUDFRONT_URL` | Target origin; otherwise the shared helper's dev default |
| `AWS_PROFILE`, `AWS_REGION`, `ENVIRONMENT` | Credentials and secret environment; default region `us-east-1`, environment `dev` |
| `E2E_TEST_USERNAME`, `E2E_TEST_PASSWORD`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID` | Optional complete credential override; otherwise Secrets Manager |

## Evidence and simulations

| Coverage | Evidence |
|---|---|
| Current `/runs`, `/activity`, `/budgets`, `/settings/connections` | Exact path and heading, current navigation, successful page-initiated real API response |
| Flag-on entry, return, direct URL, reload and browser Back | Preview/current DOM assertions and exact destination checks |
| Identity and workspace | Same actor/context claims and selected workspace before/after entry and return; tokens never recorded |
| Preview links | Same visibility as current navigation, including budget/rate permission predicates |
| Real flag-off deployment | No entry link; bookmarked preview URLs return to current UI |
| Signed-out access | `/next` and `/runs` reach actual `/login` and show email sign-in |
| **Simulated expired storage** | Real session's local expiry is set in the past and refresh token removed; requires login and cleared access token. Does not claim server-side Cognito expiration/revocation. |
| **Simulated chunk failure** | Fresh browser context, observed aborted `Next*-*.js` request, eager standalone fallback and full-document escape. A cached/normal preview is a failure, not substitute evidence. |
| **Simulated already-open-tab rollback** | Initially loads real flag-on preview, then controls only that browser's `/api/features` response to false. Requires bounded polling, preview exit, hidden entry and unchanged identity without document reload. Does not claim real SSM/deployment rollback. |

Failure screenshots contain real UI data and are private local artifacts (mode
0600); review before sharing. No HAR, trace, token storage state or raw API response
body is persisted by this suite. API paths/statuses and simulation observations
are included in JUnit properties. Rendered-record comparisons, populated multi-org
coverage, and broader negative authorization checks remain coordinator work under
#5096; an org-less test identity does not validate populated workspaces.
