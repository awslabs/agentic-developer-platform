# Grype suppression policy review — #6121

All 189 exact original configured entries are retired from the active configuration.
`decisions.json` preserves each original selector and rule, its historical section,
comment context, recorded review dates, and specific scope deficiencies.
`historical-config.txt` preserves the commented configuration used for this review.
The frozen original manifest is identified by SHA-256 in the ledger.

Expired reviews were not renewed. Rules without enforceable installed-version and
image boundaries cannot support the component-specific, version-specific or
runtime-specific claims in their comments. Retiring those rules makes findings
visible; it does not resolve or accept any vulnerability. Unpatched findings remain
owned by #6111–#6114 and #6122–#6125 according to the original image-family inventories.
No original inventory is replaced by a newer or smaller scan.

The filter continues to test exact advisory/package/ecosystem matching for future
narrow selectors. Unsupported image/version constraints fail conservatively by
retaining findings. There are currently no approved active exceptions. A future
exception needs current exact-image/version evidence and enforceable scope before
activation. Grype-native exclusions still apply and must not be confused with repository
suppression rules. Capturing their full effective configuration and database
identity in each production scan remains acceptance work under #6121.

Scanner CLI and integration tests preserve raw input bytes, filtered output,
suppression summaries, image/source identities and artifact hashes. Complete live
scan provenance and family reconciliation remain acceptance work under #6121.
