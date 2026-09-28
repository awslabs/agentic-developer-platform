# Reviewed advisory accounting

The frozen active inventory contains **59 unique Critical / 389 unique High CVEs**
after resolving the two separately reported Amazon Linux High bundles. Across
all scanned scopes, the reviewed count is **64 Critical / 425 High**. There are
now zero unresolved bundles. This is a review of the same inventory, not a new
scan or a deployment result. Pending package applicability remains open.

| Bundle | Package | Individual High CVEs | Other members retained outside C/H scope | Fixed AL2 package version |
|---|---|---|---|---|
| ALAS2-2026-3227 | python, python-libs | CVE-2026-4519 (7.1) | CVE-2025-13462 and CVE-2026-3479: Low, 2.5 each | 2.7.18-1.amzn2.0.18 |
| ALAS2-2026-3800 | expat | CVE-2026-56403 (7.7), CVE-2026-56406 (7.7), CVE-2026-56407 (7.0) | none | 2.1.0-15.amzn2.0.8 |

AWS publishes both aggregate advisory membership/fixed package versions and
individual CVE severity pages. Retained HTML and SHA256 receipts show that
Important maps to High. The earlier Grype related-CVE ratings disagreed with the
bundle rating; this explicit vendor review resolves that conflict. The original
bundle IDs, scanner High severity and installed package versions remain in the
decision records. These versions are older than the listed AL2 fixes; vendor
membership resolves their assignment, not their exploitability in the workload.

The raw register is unchanged. Counts deduplicate CVEs at the highest open
Critical/High severity within each scope, exclude the previously reviewed
vendor-unaffected row, and do not add scope totals. The normalized baseline has
4,132 rows (4,131 open); expansion replaces three bundle/package rows with nine
CVE/package rows, including four Low rows kept outside this task's scope.
The exact Debian zlib evidence from #6532 removes ten C/H occurrences; the CVE
remains on other images. The resulting C/H register has 4,123 open occurrences.
No candidate-image fixes are deducted because no live rollout was verified.

Full derived register and reconstruction script are retained in
`/workspaces/projects/security27/alas-review/`; committed decisions and input
hashes preserve the review independently of that local path. To reproduce:
expand the three specified bundle rows using bundle-decisions.json; remove the
ten exact zlib occurrence decisions; retain statuses; group open rows by
canonical ID and highest severity in each selected scope. Verify the frozen
input hash in reviewed-summary.json before applying these decisions.
