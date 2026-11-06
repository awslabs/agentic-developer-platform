# Financial document suggestions

In **Profile setup → Connections & people**, inspect an existing GitHub
connection, find the repository, and open **Connect a financial document**.
Enter the path of a JSON financial plan in that repository. Repository and
connection identities come from your selection.

In **Financials**, start or resume onboarding, then choose **Suggest values from
connected documents**. Review the suggested amount and date. Accepting the date
preserves your existing allocations and leaves the other suggestions available.
The source file is read through the installed GitHub App; it is never changed.

[Example financial plan](examples/financial-plan.json):

```json
{
  "release_target_date": "2027-01-18",
  "allocation": {
    "name": "Proposed cloud plan",
    "amount": { "amount": "100.00", "currency": "USD" },
    "category": "cloud",
    "period_start": "2026-10-01",
    "period_end": "2027-03-31"
  }
}
```

Either the date or allocation may be omitted. Amounts are exact decimal strings;
categories are `ai`, `cloud`, or `other`. The file must be at most 64 KiB and must
contain valid calendar dates and a period whose end is on or after its start.
This import supports structured JSON plans. Free-form prose, PDFs and spreadsheets
are not parsed by this adapter.

Suggestions retain the file's Git blob revision. Source access is checked again
before publishing or confirming suggestions. A provider outage or changed access
prevents a new import; it does not replace saved financial values. Document values
remain proposals and cannot approve funding, alter spending limits, create actual
costs, or perform currency conversion.

The same import is available through the onboarding guidance API with
`mode: "financial"`. CLI users can connect a document binding whose native ID is
`<repository-id>:<relative-path>`, then use `adp onboard guide --mode financial`.
Use the same idempotency key when retrying an uncertain request.
