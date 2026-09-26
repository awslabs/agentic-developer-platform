# Credentials and linked identities

`adp credential list` and `adp credential show ID` read visible metadata through
the vault API. They never retrieve secret values or Secrets Manager ARNs.
`--scope user|team|org|domain_app` filters the visible list; server permissions
remain authoritative. The current API returns an unpaginated list.

Create using a stable UUID and protected input:

```sh
adp credential add --service example --type api_key --label automation \
  --operation-id 21f2eb5c-4749-40f1-90da-c10016c342cc --value-file ./private-value \
  --yes --json
```

The file must be owned by you, mode 0600, regular and not a symlink. `--value-stdin`
preserves multiline input including the final newline; without either option the
CLI prompts with hidden input on a terminal. Secret values cannot be supplied as
flags and are never saved in CLI state. Keep the operation UUID and exact inputs
for reconciliation; an uncertain response is not success. Never generate a new
UUID merely because the original response was lost. The server validates replay
under the same operation UUID; conflicting requests are refused.

Supported types: api_key, oauth_token, basic_auth, bearer, ssh_key, certificate,
config_file and aws_role. AWS provisioning/verification remains `adp aws connect`.
Scope defaults to user; shared scopes require server authorization and domain_app
requires `--domain-app-id`. `--dry-run` reads no secret and makes no mutation.

```sh
adp credential update ID --label renamed --dry-run --json
adp credential update ID --label renamed --expected-revision TIMESTAMP --yes --json
adp credential delete ID --dry-run --json
adp credential delete ID --yes --json
```

Updates preserve the reference and change only label, expiry or strict metadata.
Use the revision from show/dry-run. A stale revision returns 409; an older gateway
without the revision-aware endpoint returns 404 instead of ignoring the condition.
Independent secret rotation is unsupported by this API; the CLI does not implement
a delete/recreate workaround. Deletion removes the vault entry and schedules the
AWS secret's recovery-window deletion. It does not stop running jobs. This API
does not enumerate downstream dependencies. A repeated delete returns not found;
it does not claim the earlier attempt succeeded.

```sh
adp identity list --provider github --json
adp identity link --provider github --provider-user-id ACCOUNT_ID --dry-run --json
adp identity link --provider github --provider-user-id ACCOUNT_ID --yes --json
adp identity link --provider github --provider-user-id ACCOUNT_ID --resume --json
adp identity unlink ID --provider github --dry-run --json
adp identity unlink ID --provider github --yes --json
```

Link records an **unverified claim**, returns pending (exit 4), and does not confer
provider access. Complete the platform's provider sign-in or administrator
verification, then resume to read its result. No consent token is synthesized or
printed. Existing server ownership and unlink rules apply; other linked identities
and user sessions are preserved. Supported providers are github, slack, whatsapp
and discord. CLI tenant selection uses the shared authenticated context.

Nightly EC2 regression E24 covers metadata reads and dry-run behavior without
creating credentials or provider claims. Full lifecycle/consent, stale-write,
revocation and owned-fixture cleanup acceptance remains required before #5631 closes.
