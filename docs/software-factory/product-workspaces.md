# Product workspaces

The product page uses **Overview · Build · Release · Operate · Insights · Agents · Knowledge · Budget & spend**. Projects and Product settings remain in the header. The profile summary is shown only on Overview.

## Navigation and existing work

All workspaces use `/products/:productId/projects/:projectId` as their base path. Workspace selection is in the URL, so refresh and browser history retain the destination. Legacy `/flows`, `/services` and `/services/:service` links remain supported.

| Workspace | Work available here |
| --- | --- |
| Build | Delivery flows and Agent activity. Both views use the selected project. Agent activity reuses the main navigation screen: filters, run/chain grouping, live task checklist, run details and retained transcripts. Native service histories remain under their agent pages. |
| Release | Prepare, Deploy and Verify. Preparation links to threat modeling, full repository code scanning and architecture review. Deployment, regression-test triggering, release readiness and release verification are explicitly unavailable where ADP has no supported execution path. |
| Operate | Monitor and Detect context, investigation for Triage, code changes for Fix, and cost/architecture review for Improve. Native DevOps inspection retains its existing consent and ADP SSO controls. |
| Insights | Delivery performance, Quality & acceptance and Engineering outcomes. Definitions and required evidence are accessible without starting data collection. Published business-context evidence is read from the existing authorized Engineering Insights endpoint. |
| Agents | All five AWS agents with capability descriptions, availability, shared setup, runs, supported schedules and retained results. |
| Knowledge | Existing product-scoped knowledge. |
| Budget & spend | Project budget, reservations and spend, also available directly in Projects with links to project delivery flows and activity. Existing configured budgets are preserved. |

Agent detail pages have three destinations: **Setup**, **Runs & schedules**, and **Results**. Setup includes capability selection. The intermediate service-selector tabs are removed. The existing forms remain mounted when moving between these destinations; hidden invalid fields are revealed before saving.

Existing version checks, idempotency receipts, readiness checks, source revision selection, owner consent and admission controls are retained. An activity link selects context; it does not start work. Security capability links preselect only supported, saved capabilities.

## Scope and evidence

- Delivery flows, activity, native agent controls and Insights evidence use the selected project. New flow and conversation links carry both product and project. The global navigation retains the wider activity and delivery views. Activity discovery uses immutable project execution attribution and dispatched run assignments; it does not infer ownership from repository names or correlation IDs. Historical runs without that attribution remain in global activity.
- Current product-team membership is checked for each shared activity read, including every chain member and retained transcript. Live explanations recheck membership during the connection; run commands retain their existing owner authorization. Project chain grouping groups the current page; opening a chain reads its authorized members.
- Project budgets cover total metered AI usage shared by flows and agents. Each request reserves funds before execution, and retries retain attribution to the same project. Per-run and other limits still apply. Cloud infrastructure charges are separate. Only the product owner or a platform administrator can edit a project budget.
- Task boards are agent-reported progress. They do not establish engine acceptance or successful delivery. Earlier attempts remain accessible from the flow and activity history.
- The Insights results reader currently exposes published business-context work and cost associations. These are not shipped-story counts, DORA metrics or acceptance-criteria coverage.
- DevLake metric aggregates and the six-metric outcome scorecard are not exposed by this UI's existing APIs. The UI labels them as awaiting integration and explains the required source evidence; it does not display mockup values or interpret missing data as zero.
- Saved connections are distinct from analytics collection coverage. Opening Insights does not authorize historical collection.
- Pen testing, unsupported schedules and unavailable native execution paths remain visibly unavailable. No new cloud execution capability is enabled by these pages.

## Validation

Run from `modules/gateway/frontend`:

```sh
npm run build
npx vitest run --maxWorkers=4
npx playwright test --config tests/e2e/product-workspaces.config.ts
```

The Playwright suite uses only an isolated localhost build, synthetic identity and mocked API responses. Requests outside its localhost origin are blocked. It covers navigation, keyboard controls, refresh, five viewport widths, Security threat modeling and full repository code scanning, a daily UTC schedule save/readback, DevOps readiness and investigation, native access gating, retained results and Insights evidence errors. It also covers project budget save/readback, project flow creation context, and live activity plus transcript inspection at desktop and mobile widths. `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` can select an already installed test browser.

Service unit tests also cover the existing five-agent setup, scheduling eligibility, version conflicts, scope changes, revocation, uncertain writes and run/result controls. These checks validate UI behavior and API contracts, not the state of a live AWS account or deployment.
