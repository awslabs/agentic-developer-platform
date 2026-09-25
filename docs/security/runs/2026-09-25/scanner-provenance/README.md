# Scanner provenance validation — #6121

The rebuilt agent-mail cache image was scanned once with Grype 0.119.0, emitting SARIF and a JSON descriptor from the same acquisition. The repaired filter CLI preserved the raw bytes and produced the filtered report and suppression summary. The production metadata helper extracted the scanner version/timestamp, database schema/build/providers and SHA256 of the actual database, plus effective matching configuration and all four native kernel-header exclusions. Registry configuration, database filesystem paths, and the full private descriptor are not published.

The result contains **386 raw SARIF results**, with **zero repository suppressions** after the 189-rule retirement. Native Grype matching exclusions remain explicitly recorded. This is scanner acceptance evidence, not a clean image claim; image-family remediation remains separate.

`validation.json` records the exact image ID, build source and artifact hashes. Raw and filtered SARIF remain locally at `/workspaces/projects/security25/scanner-real-validation/`; the small sanitized metadata and summary are committed here. Production scanner runs upload these artifacts and hash metadata in coverage and provenance. Missing metadata fails target coverage.
