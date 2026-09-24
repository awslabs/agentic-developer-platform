# Evidence-led cyber analyst

The analyst returns one assessment across browser observations, retrieved archive
pages and attributed sources: `clean`, `suspicious`, `malicious` or `inconclusive`.
Collection failures leave assessment pending; they do not select a verdict.
Confidence is qualitative and optional. Schema, reference and integrity checks
remain, without semantic finding or verdict gates.

Navigation accepts relevant public URLs with a short reason. Reviews are optional.
Screenshots are requested separately so screenshot timeouts do not discard page
text. Same-run case observations can be imported as provenance-linked sources;
previous verdicts and reviews are excluded. Private destinations, form submission,
credential entry, downloads and challenge bypass remain outside the browser API.
The guarded transport remains until a native alternative is validated.

The default startup/action/navigation/screenshot/session budgets are respectively
120/90/45/5/600 seconds. `runtime_limits.py` lists their environment overrides.
Athena has separate queue (300 seconds) and execution (120 seconds) budgets,
configurable through `CYBER_CC_QUEUE_SECONDS` and `CYBER_CC_EXECUTION_SECONDS`.
Discovery supports exact URL, hostname and IP searches across up to twelve
configured crawl partitions; archive reads require matching worker IAM grants.
RDAP queries the registrable parent using an offline public suffix list. URLhaus
lookup is optional and needs `CYBER_URLHAUS_AUTH_KEY`; missing credentials are
reported for that provider. No submission for remote scanning is performed.

## Same-evidence comparison

Run `replay_assessment.py` inside an authorized AWS worker, using its normal S3
permissions. It never uses operator credentials to download a dataset. The S3
manifest has `cases` with `id`, `case_uri`, and the pinned case JSON `sha256`.
Each case directory must have its integrity manifest and closed browser state.

```sh
python /app/skills/url-analysis/replay_assessment.py \
  --manifest s3://EVIDENCE_BUCKET/RUN/replay-manifest.json \
  --baseline-prompt /comparison/baseline-skill.md \
  --baseline-schema /comparison/baseline-schema.json \
  --model MODEL_ID \
  --output s3://EVIDENCE_BUCKET/RUN/replay-results.json
```

Both arms get identical verified observations, sourced context, extracted archive
text, inert DOM and screenshots. Prior assessments, reviews, hypotheses, incident
narratives and reference labels are excluded. Package and evidence hashes, model,
inference settings, usage, raw assessments and validation errors are retained.
Output is uploaded and read back after each case. Arm order alternates.

This compares the URL skill and assessment schema as a package. It does not test
interactive persona behavior, tool selection, navigation, or detection accuracy.
Single samples show sensitivity; human evidence review and repeat trials are
needed for claims about correctness or significance. Historical May verdicts are
comparisons, not ground-truth labels. Browser collection needs a separate run.
