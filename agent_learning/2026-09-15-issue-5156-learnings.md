# Issue #5156 — qualification config + fixture lifecycle

Branch `agent/issue-5156`, draft PR #5229. Offline harness only: no live
qualification was run and no environment was provisioned.

## The bug that mattered most: a green run that tested nothing

`python -m pytest tests/e2e/orchestration` reported 199 passed. `python -m
pytest tests/` reported **258 skipped, 0 passed — and exited 0**.

Cause: `tests/e2e/chat/conftest.py` and `tests/e2e/infra/conftest.py` both do

```python
for item in items:            # every item in the SESSION, not their package
    item.add_marker(skip_marker)
```

when `E2E_CHAT_ENABLED` is unset. Collect them in the same session as any other
package and that package is skipped too. `tests/e2e/new_ui/conftest.py` already
had the fix — it filters to `own_items` via `Path(item.path).is_relative_to(PACKAGE)`
— so the precedent existed and the two older conftests simply predate it.

The subtle part: copying the `own_items` filter into a plain
`pytest_collection_modifyitems` **was not enough**. Hook call order is not
defined by directory depth, and the sibling hooks ran *after* mine, re-adding
the skip marker I had just removed. The fix is a hookwrapper:

```python
@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(config, items):
    yield                      # let every other implementation finish first
    ...remove the inherited skip from this package's items...
```

Generalisable rule: **if your hook needs to be the last word, `hookwrapper=True`
+ `yield` is the only reliable way to say so.** A plain hook competing with
another plain hook is a coin flip that varies with collection order.

Second-order lesson: this class of failure is invisible because pytest exits 0
on an all-skipped run. Two defences, both cheap:
- a CI step that parses the JUnit XML and fails when `total - skipped == 0`
  (pattern lifted from `e2e-new-ui-playwright.yml`);
- a test that reads `conftest.py` and asserts `hookwrapper=True` is still there.
Without the vacuity check the CI job would have gone green on zero tests, which
is worse than having no job at all.

## Facts about the repo-root `tests/` tree

Worth knowing before adding anything there — I had to find each one the hard way:
- **No PR CI job covers it.** Nothing ran these tests on a pull request, which is
  why this change had to create `orchestration-harness-ci.yml` from scratch.
- **No ruff config, no ruff CI run.** I ran ruff manually as a pre-submit check;
  its findings are advisory here, so each one needed a judgement call rather
  than blanket compliance.
- **No requirements file.** There is no dependency install to hook into, so the
  harness is stdlib + pytest only. `jsonschema` and `pyyaml` are both absent —
  hence a hand-written config validator, and a line-based reader in
  `test_ci_selection.py` instead of parsing YAML.

`boto3` is imported *lazily*, inside the runtime secret-resolution path and
`_caller_identity()` only, so the offline tests never need it. The CI job
deliberately does **not** install it: if a test ever starts requiring AWS, the
job fails instead of quietly reaching the network.

## Design notes worth reusing

**Write-ahead ordering, and why resume must ask.** Record intent → call provider
→ record observed id. A crash between steps 2 and 3 is *indistinguishable on
disk* from a crash between 1 and 2: both leave a `planned` entry. So `--resume`
cannot infer anything from its own records — it has to ask the provider what
exists. That single observation drove the whole module shape.

**Derive idempotency tokens, never randomise them.** `f"{qual_id}-{fixture_id}"`
recomputes identically on resume, so a provider honouring the token returns the
original resource instead of creating a twin. A random token would make every
resume a fresh leak.

**Failures default to "not owned".** `verify_ownership` returns False for a
failed tag read, an untagged resource, and any tag mismatch. Deleting on an
*unreadable* tag read is how a cleanup job destroys someone else's resource, and
that path is reached exactly when the provider is already misbehaving.

**Let the ceiling catch the typo.** `BOUND_CEILINGS` refuses `max_usd: 10000`
before anything can spend it. Validating that a bound is *present* is not the
same as validating it is *sane*.

**Distinct exit codes carry the safety property.** Exit 4 = "nothing ran",
separate from 0 = pass, so an empty adapter registry can never look like a
success — the issue's central requirement, expressed where CI can act on it
without parsing prose.

**Report every config problem at once.** One fix-and-rerun cycle per typo is a
bad operator experience when the config has a dozen fields.

## Process

- My own tests found a real bug in shipped code: the secret-key scanner rejected
  the legitimate `secret_refs` section because the exemption compared the parent
  path instead of the child. Worth noting that the *tests* caught it, not review.
- Two tests were quietly worthless until fixed: `write_config` merged dicts, so
  "missing required key" cases were no-ops (needed a `remove=` parameter), and
  one test asserted `record is None` on a variable that was unconditionally
  None. A test that cannot fail is worse than a missing test — it reads as
  coverage.
- A workflow assertion matched the word "secret" inside a prose comment about
  secret handling. Assert on structure (declared input *names*), not on
  substrings of a file that legitimately discusses the thing being banned.
- `pytest.raises(Exception)` passes on `ImportError`, `AttributeError`, or a
  typo in the test itself. Both instances now name `FixtureError` with a
  `match=`.
- The reviewer workflow triggers on the `agent-reviewer` **label**, not on PR
  open, so opening a draft PR early is free — checked before doing it.
- Ruff's `--fix` for `UP035` appended the moved imports out of alphabetical
  order. Autofixes still need reading.

No credential, key or token appears in this file, the config example, the
workflows, or the inventory format. The one AWS-shaped string in the tests is
AWS's own published `AKIAIOSFODNN7EXAMPLE` documentation placeholder, used as
input to prove the detector fires.
