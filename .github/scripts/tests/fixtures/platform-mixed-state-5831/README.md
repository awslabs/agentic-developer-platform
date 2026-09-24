# `platform-mixed-state-5831/` — mixed-age state reproduction

Backs `test_platform_mixed_state_export.py` (issue #5831, parent epic #3959).

Two files: `main.tf`, a reproduction configuration shaped like the platform root,
and `mixed-age-state.json`, a Terraform state record carrying resource records
of **two different ages**.

## Provenance rule — written from observed failures, not from tooling output

Both files were written from the **error messages and state facts reported on
issue #5831**: the observed record contents (`namespace_config`, the
`account_id, addon_name, cluster_name, region` identity fields, `serial` 84,
Terraform 1.14.6) and the two distinct export failures reproduced against them.
Field *shapes* were then checked against `terraform providers schema -json` for
the provider the platform floor resolves.

Neither file was captured from a real state backend, and nothing here came from
the real dev account. Every value is synthetic: account `000000000000`,
`lt-0123456789abcdef0`, placeholder ARNs. The real state is private to root and
must not be committed — which is the constraint that makes a synthetic fixture
the only option, and therefore makes this provenance rule load-bearing.

## Why two ages of record, and what breaks if that drifts

| Record | Age | Why it is here |
|---|---|---|
| `aws_eks_addon.coredns` | **Newer** than provider 5.x | Carries `namespace_config` and the four identity fields. Provider 5.x publishes no resource identity schema for this type, so it could not be serialised to JSON at all — the original defect, and the reason the floor is 6.42.0. |
| `aws_launch_template.gvisor_nodes` | **Older** than provider 6.16.0+ | `schema_version` 0 while the resolved provider declares version 1. This is the record that still broke the targeted export *after* the floor was raised. |

**The first attempt at #5831 shipped a fixture with only the newer record.** It
passed on the raised floor while the real targeted plan still failed. That is the
specific failure this fixture exists to prevent, so
`test_fixture_carries_both_ages_of_state_record` pins both records, and pins the
stale one at `schema_version` 0 — raising it would make the suite pass by
deleting its own subject.

## Details that matter

- **`main.tf` deliberately declares no `aws_eks_addon`.** The real platform
  source declares none while the real state records one. That mismatch is why a
  full-scope plan proposes *deleting* the add-on, which is the reason the
  documented migration must be a saved `-refresh-only` plan and never an ordinary
  full apply. Declaring it here would quietly remove the hazard under test;
  `test_fixture_does_not_declare_the_add_on_it_records` prevents that.

- **The provider constraint in `main.tf` is the placeholder
  `AWS_VERSION_CONSTRAINT`,** substituted by the test. The suite compares provider
  versions against one identical state record, so the version must be the
  variable, not a second thing baked into the fixture.

- **The `_comment` key in the state file is inert.** Terraform ignores unknown
  top-level state keys. It is a literal string array, not a state field, and no
  assertion reads it.

- **`serial` is 84 and `terraform_version` is 1.14.6** to match the preservation
  snapshot described on the issue. Nothing asserts on them; they are there so the
  fixture is recognisable as a stand-in for that specific observation rather than
  an arbitrary record.

- **The state fixture is named `mixed-age-state.json`, not `*.tfstate.json`.**
  `.gitignore` line 23 ignores `*.tfstate.*`, which exists to stop real state
  being committed. That rule is worth keeping intact, so the fixture is named
  around it rather than force-added past it with `git add -f` — a force-add would
  have to be repeated by every future editor and teaches the wrong habit.

- **No credentials are needed or used.** `main.tf` disables credential validation
  and the suite strips every `AWS_*` variable, because the worker and CI pool both
  run with real credentials available. An "offline" test that could silently read
  a real account would be worse than no test.
