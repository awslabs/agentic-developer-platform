# Verified gateway rollout and refreshed observed inventory

PR #6533 merged and Gateway Deploy run36311105832 succeeded, including schema
migrations, all3 ready updated replicas, pricing verification and the authenticated
post-deploy smoke. The exact observed amd64 digest23db69c8109c was independently
scanned: **0 Critical /50 High native matches**, versus16/70 on the frozen baseline
gateway image. The existing exact Debian zlib unaffected review (#6532) was
rebound to the actual installed binary and package version, leaving **0/49**
reviewed gateway matches. This is the main bedrockgateway deployment only;
the separate authority probe still runs an older gateway image.

The refreshed10:09UTC inventory contains31 observed active digests. Each maps to
a verified baseline platform scan or the new gateway scan. Reviewed active totals
remain **59 unique Critical /389 unique High**, because the removed CVEs remain on
other images. Open active image/package occurrences fall from3231 to3195 (**36
fewer**); these are not36 unique CVEs. Count each CVE at its highest severity in
the active scope, so Critical/High severity overlaps are not added twice.

The snapshot also has53 container rows with no runtime imageID during runner
churn,42 incomplete owner chains and6 desired-template rows without an active
observation. These are coverage gaps, not zero findings. The counts describe the
observed active digest set; they are not a claim to cover pending executions,
unknown launcher templates, node packages or external compute. Original baseline,
raw scans, lower severities and historical/desired-only findings remain intact.

`active-inventory-summary.json` binds the private collection receipt, immutable
baseline mappings, exact new scan and zlib binary evidence. `reconcile_live.py`
reproduces the join against retained local audit inputs; it fails if an observed
image lacks a scan mapping. The frozen all-scope baseline64 Critical/425 High is
historical evidence and is not relabeled as a new all-scope live total. No source
candidate was deducted without an observed rollout.
