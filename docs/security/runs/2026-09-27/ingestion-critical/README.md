# Ingestion Critical remediation

Desired idle ingestion workloads add Ruby zlib CVE-2026-27820, Ruby Net::IMAP CVE-2026-42257, and libxml2 CVE-2026-6653 to the observed-image audit scope. Debian's current tracker still marks the installed packages vulnerable; original findings remain open until deployment and exact-image review.

The normal image build now uses digest-pinned Ruby 3.4.11, including zlib 3.2.3 and Net::IMAP 0.5.15, while retaining Bundler and Sorbet. It also replaces the ABI-2 libxml2 package with upstream 2.13.9, authenticated against the project's published SHA256, retaining its copyright and package identity. The project revision contains the exact vendor-referenced fix commit. The full libxml2 test suite and 2,166 fuzz-corpus inputs passed.

Native nonroot Ruby compression, Bundler, Sorbet type checking, LLVM dynamic linking, and Chromium DOM/canvas/screenshot checks passed. The normal build also retains the authenticated curl packages and licenses. The final candidate has raw 25 Critical /154 High and reviewed 0 Critical /124 High. No new Critical/High advisory-package pairs were introduced. Scan and exact review receipts are committed; publication and live promotion remain separate.

Deployment resolves ingestion selectors to verified ECR digests and applies that same image to the enabled vulnerability scanner CronJob, whose old tag no longer exists.
