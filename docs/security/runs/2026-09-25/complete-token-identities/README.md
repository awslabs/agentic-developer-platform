# Complete detect-secrets candidate identities

Follow-up: #6110. This changes scanner correctness and evidence handling; it does
not establish that any underlying credential has been remediated.

The pinned detect-secrets 1.5.0 GitHub matcher returns its capturing prefix group
instead of the token. Its JWT matcher stops before the signature. Two synthetic
GitHub tokens with the same prefix and two synthetic JWTs with the same payload
therefore produce only two identities upstream. The repository launcher produces
four complete-token identities through both the real scan and audit paths. JWT
header/payload validation retains the upstream detection scope, including example
signatures that are not valid base64. No candidate is authenticated or verified.

The launcher applies the correction temporarily in memory and pins its package
version. Scan and audit carry the same versioned matcher policy and launcher
SHA256. New audit artifacts use `adp.detect-secrets.audit/v2`: candidate SHA1,
filename, detector types, category, and line numbers with redacted values. Raw
candidates, source snippets, native diagnostics, and exception text are withheld.
SHA1 follows the native evidence schema; it is an identity, not encryption.

Coverage, reconciliation, and diffing join on candidate hash as well as location
and detector. Distinct candidates on one line remain distinct. Historical private
audit artifacts remain readable without changing their schema or original bytes.
When comparing an old baseline to the corrected policy, GitHub/JWT identities are
reported separately as pending legacy review, never counted as resolved merely
because their hashes changed. Legacy false-positive labels do not transfer to
new complete-token identities.

The frozen source and original 1,859 scan records / 1,849 overlapping audit groups
are unchanged. Eight GitHub and twelve JWT original records contain partial
captures; those 20 underlying source candidates still need complete-token review.
The original selector ledgers remain authoritative for that original acquisition.

Validation: 93 focused tests pass across `test_complete_secret_identities.py`,
`test_diff_security_findings.py`, and `test_security_scan_tool_invocations.py`,
using detect-secrets 1.5.0. Tests exercise real plugins, offline scan/audit,
same-line coverage loss, reconciliation, diff CLI/report generation, historical
schema compatibility, policy mismatch/downgrade rejection, and output redaction.
All token fixtures are synthetic. No original candidate credentials are exercised.
