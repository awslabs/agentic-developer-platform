# External activity read limits (#6934)

The delegated work summary reads GitHub and GitLab events only after verifying the
chatting person's linked provider identity and current repository membership. It
does not create an activity index. The feature is off unless the gateway has
`ADP_EXTERNAL_ACTIVITY_ENABLED=true`; the worker receives no provider credential.

- A request accepts a timezone-aware window of at most 31 days. It checks at
  most two GitHub installations and ten repositories per installation, or ten
  configured GitLab projects. Limits produce partial coverage, not an empty-work
  claim. The ADP activity index has a separate 30-day retention warning.
- Provider activity uses at most 100 records per page, three pages per endpoint,
  and a shared budget of 50 activity page requests per provider per work-summary
  request. Pagination follows the provider's continuation signal, including
  empty pages with a next page. GitHub commits, PRs, PR reviews and issue
  comments and GitLab commits, MRs and issue/MR events are queried read-only.
  GitLab comments use the nested `Note` payload's issue/MR reference and link
  directly to the note. System notes and assignments are not human work;
  malformed note references produce partial coverage rather than guessed links.
  Authorization, identity, token minting and installation enumeration require
  additional bounded requests outside the activity-page budget.
- Reaching a page/request limit, an unavailable provider or a rate limit leaves
  returned authorized events intact and marks history incomplete. Provider APIs
  can omit older history: GitHub reviews of PRs not updated since the window began, for
  example, are not enumerated. GitLab commits without a confirmed linked email
  are excluded. A provider's own retention and API availability also limit what
  can be observed; neither an empty page nor a current issue state proves no
  earlier activity occurred.
- Provider requests use server-constructed HTTPS destinations and never follow
  redirects. Source links from responses must match the expected provider,
  repository and action path; invalid links are omitted with partial coverage.
  Local and link-local GitLab destinations are rejected even if configured.
- Reads are fresh on the first work-summary page; subsequent ADP cursor pages
  label external coverage `continuation_only`. Each response carries its
  observation time. No provider event or authorization result is cached in ADP;
  each request rechecks access, so revocation cannot expose previously read data.
