"""The migration lane refuses, and can execute — Issue #5042 (U3), PR #5283 finding 6.

## What this suite has to prove, and why the obvious version of it would not

Finding 6 raised two failures that pull in opposite directions:

  * the lane **could not execute its contract** — its apply step raised an unconditional
    error even when every preflight had passed, so an operator who satisfied every gate
    still could not migrate; and
  * the lane's **checks were not checks** — the backup "requirement" was an
    `echo "::warning::"` (a warning does not stop a step), the plan step printed the strings
    `alembic current` / `alembic history` without running anything, and the database boundary
    was a grep over SQL text.

A suite that only asserted "the blocked paths exit non-zero" would pass against the code the
review rejected, because that code exited non-zero on everything. So every refusal here
asserts the REASON, and a matching set of tests asserts that a run with every input satisfied
reaches `kubectl apply` — against a stub `kubectl`, since that is the property the previous
version lacked.

## The reproduced escapes are tests, not prose

`TestTheTextScanCouldNotHaveWorked` runs the *old* guard's exact pattern over the review's
three evasions and asserts two of them pass it. Without that, the rest of this file proves
only that some new mechanism rejects some things — not that it replaced something broken. The
denylist it builds is derived from the gateway's real `__tablename__` declarations, the same
way the old guard did, so it cannot be accused of testing a strawman list.

## Why subprocesses

The scripts are the lane's actual entry points; the workflow calls them with argv and reads
exit codes. Importing their functions would test a different interface than the one that runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".github" / "workflows").is_dir():
            return candidate
    raise AssertionError("could not locate the repository root from this test file")


REPO_ROOT = _repo_root()
MODULE_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = MODULE_ROOT / "infra" / "scripts"
CONTRACT = SCRIPTS_DIR / "check_migration_contract.py"
PLAN_POD = SCRIPTS_DIR / "plan_pod_from_job.py"
RENDERER = SCRIPTS_DIR / "render_manifests.py"
MIGRATIONS_DIR = MODULE_ROOT / "migrations"
JOB_SOURCE = MIGRATIONS_DIR / "job.yaml"
LOCK_FILE = MODULE_ROOT / "releases" / "superplane.lock.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "superplane-migrate.yml"

NAMESPACE = "superplane"
SCHEMA = "superplane"
ACCOUNT = "879318057152"
RUN_ID = "1234567890"

SHIPPED_LOCK = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))

# A promoted `superplane-api`, for the tests that need to get past the pending-image refusal.
# Constructed rather than copied from the lock, because the lock does not pin one — that is
# the blocked state. The digest is obviously synthetic so it can never be mistaken for a real
# pin if it leaks into an error message.
PROMOTED_DIGEST = "sha256:" + "cd" * 32
PROMOTED_REGISTRY = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"
PROMOTED_REPOSITORY = "adp-superplane-api"
PROMOTED_REF = f"{PROMOTED_REGISTRY}/{PROMOTED_REPOSITORY}@{PROMOTED_DIGEST}"


def _unblocked_lock(**mutations) -> dict:
    """The shipped lock with both blocks cleared — the state after #5045 and U2 finish.

    Derived from the real lock rather than written from scratch, so a change to the lock's
    shape surfaces here rather than leaving these tests asserting against a structure the
    scripts no longer see.
    """
    lock = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))
    lock["schema"]["single_head"] = True
    lock["schema"]["status"] = "verified"
    lock["pending_images"].pop("superplane-api", None)
    lock["images"]["superplane-api"] = PROMOTED_DIGEST
    lock["image_sources"]["superplane-api"] = {
        "registry": PROMOTED_REGISTRY,
        "repository": PROMOTED_REPOSITORY,
    }
    for key, value in mutations.items():
        lock[key] = value
    return lock


def _write_lock(tmp_path: Path, lock: dict, name: str = "lock.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
    return path


def _run_contract(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONTRACT), *args], capture_output=True, text=True
    )


def _contract(
    *,
    lock: Path = LOCK_FILE,
    migrations_dir: Path = MIGRATIONS_DIR,
    job_file: Path | None = None,
    evidence: Path | None = None,
    require_evidence: bool = False,
    namespace: str = NAMESPACE,
    schema: str = SCHEMA,
    emit_image: Path | None = None,
) -> subprocess.CompletedProcess:
    args = ["--lock-file", str(lock), "--migrations-dir", str(migrations_dir)]
    if job_file is not None:
        args += [
            "--job-file",
            str(job_file),
            "--namespace",
            namespace,
            "--schema",
            schema,
        ]
    if evidence is not None:
        args += ["--evidence-file", str(evidence)]
    if require_evidence:
        args.append("--require-evidence")
    if emit_image is not None:
        args += ["--emit-image", str(emit_image)]
    return _run_contract(*args)


def _render_job(tmp_path: Path, **env_overrides) -> Path:
    """Render the real Job through the real renderer, in the migration lane."""
    output = tmp_path / "rendered"
    env = {
        **os.environ,
        "SP_NAMESPACE": NAMESPACE,
        "SP_DATABASE_SCHEMA": SCHEMA,
        "SP_AWS_REGION": "us-east-1",
        "SP_MIGRATION_IMAGE": PROMOTED_REF,
        "SP_MIGRATION_RUN_ID": RUN_ID,
        **env_overrides,
    }
    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--source-dir",
            str(MIGRATIONS_DIR),
            "--output-dir",
            str(output),
            "--lock-file",
            str(LOCK_FILE),
            "--lane",
            "migration",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"rendering the shipped Job failed:\n{result.stdout}\n{result.stderr}"
    )
    return output / "job.yaml"


def _mutate_job(
    job_path: Path, tmp_path: Path, mutate, name: str = "mutated.yaml"
) -> Path:
    job = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    mutate(job)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    return path


def _container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["containers"][0]


def _strip_comments(text: str) -> str:
    """Non-comment lines only.

    Several assertions here are about what the lane DOES, and the lane's header documents the
    defects it replaced by quoting them — `${{ inputs.confirm }}`, `::warning::`,
    `migrations/versions`. A textual check that cannot distinguish a description of a mistake
    from the mistake itself pressures the next person to delete the explanation to get green,
    which is the opposite of what these comments are for.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _workflow_body() -> str:
    return _strip_comments(WORKFLOW.read_text(encoding="utf-8"))


def _set_env(job: dict, name: str, value: str) -> None:
    for entry in _container(job)["env"]:
        if entry.get("name") == name:
            entry.pop("valueFrom", None)
            entry["value"] = value
            return
    _container(job)["env"].append({"name": name, "value": value})


GOOD_EVIDENCE = {
    "target": {
        "identifier": "adp-dev-superplane-db",
        "endpoint": "adp-dev-superplane-db.abc123.us-east-1.rds.amazonaws.com",
        "database": "superplane",
        "schema": SCHEMA,
        "observed_by": "aws rds describe-db-instances --db-instance-identifier adp-dev-superplane-db",
    },
    "backup": {
        "identifier": "adp-dev-superplane-db-premigrate-20260917",
        "source_identifier": "adp-dev-superplane-db",
        "created_at": "2026-09-17T09:14:00Z",
        "status": "available",
        "verified_by": "aws rds describe-db-snapshots --db-snapshot-identifier adp-dev-superplane-db-premigrate-20260917",
    },
}


def _evidence(tmp_path: Path, mutate=None, name: str = "evidence.json") -> Path:
    payload = json.loads(json.dumps(GOOD_EVIDENCE))
    if mutate is not None:
        mutate(payload)
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The reproduction. Everything below only matters if this is true.
# ---------------------------------------------------------------------------


class TestTheTextScanCouldNotHaveWorked:
    """The old guard's own pattern, over the review's three evasions.

    This is the load-bearing test of the whole change: it establishes that the mechanism
    being replaced did not do the job it appeared to do. Asserting only that the NEW
    mechanism rejects things would leave open the possibility that the old one was fine.
    """

    # The pattern the previous version of superplane-migrate.yml ran, verbatim from the step
    # it lived in. Kept here as the specimen — this is not a pattern anything still uses.
    OLD_PATTERN = r"(alter|drop|truncate|rename)[[:space:]]+table[[:space:]]+[\"'`]?({tables}|alembic_version)\b"

    ESCAPES = {
        "alembic_python_api": 'op.drop_table("users")',
        "schema_qualified": "op.execute('ALTER TABLE public.\"request_logs\" DROP COLUMN cost')",
    }
    CAUGHT = {"double_space": 'op.execute("ALTER  TABLE budget_configs RENAME TO x")'}

    @staticmethod
    def _gateway_tables() -> list[str]:
        """Derived the way the old guard derived it, from the gateway's own models."""
        source = REPO_ROOT / "modules" / "gateway" / "src"
        if not source.is_dir():
            pytest.skip("the gateway source tree is not present in this checkout")
        names: set[str] = set()
        for path in source.rglob("*.py"):
            names.update(
                re.findall(
                    r"__tablename__\s*=\s*[\"']([a-z_]+)[\"']",
                    path.read_text(encoding="utf-8", errors="ignore"),
                )
            )
        return sorted(names)

    def test_the_denylist_really_does_contain_the_table_the_escape_drops(self) -> None:
        """`users` is on the list. The escape is not a gap in the list — it is a gap in grep.

        Stated separately because the natural reading of "the scan missed
        op.drop_table('users')" is "somebody forgot to list users", which would make this a
        maintenance problem rather than a design one. It is a design one.
        """
        tables = self._gateway_tables()
        assert len(tables) > 20, (
            f"expected the gateway's real table set, derived {len(tables)}"
        )
        assert "users" in tables, (
            "the reproduction depends on `users` being on the derived denylist; if the gateway "
            "renamed it, pick another table the escape uses"
        )

    @pytest.mark.parametrize("label", sorted(ESCAPES))
    def test_the_old_pattern_does_not_catch_the_escape(
        self, label: str, tmp_path: Path
    ) -> None:
        tables = "|".join(self._gateway_tables())
        migration = tmp_path / "001_escape.py"
        migration.write_text(
            f"def upgrade():\n    {self.ESCAPES[label]}\n", encoding="utf-8"
        )

        result = subprocess.run(
            ["grep", "-rniE", self.OLD_PATTERN.format(tables=tables), str(migration)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"the old text scan DID catch {label!r}. If grep's behaviour changed, this "
            f"reproduction no longer demonstrates the finding and the replacement mechanism "
            f"below needs a different justification:\n{result.stdout}"
        )

    @pytest.mark.parametrize("label", sorted(CAUGHT))
    def test_the_old_pattern_catches_only_the_naive_case(
        self, label: str, tmp_path: Path
    ) -> None:
        """The control. Without it, the tests above could pass because the pattern never
        matches anything at all — which would be a broken reproduction, not a finding."""
        tables = "|".join(self._gateway_tables())
        migration = tmp_path / "001_caught.py"
        migration.write_text(
            f"def upgrade():\n    {self.CAUGHT[label]}\n", encoding="utf-8"
        )

        result = subprocess.run(
            ["grep", "-rniE", self.OLD_PATTERN.format(tables=tables), str(migration)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "the old pattern matched nothing at all, so the escapes above prove nothing"
        )

    def test_no_workflow_still_relies_on_scanning_migration_sql(self) -> None:
        """The scan is gone, not supplemented.

        Kept alongside it, a scan reads as a second layer of defence — which invites relying
        on it for the cases the boundary does not cover, and it covers almost none of them.
        """
        body = _workflow_body()
        assert "__tablename__" not in body, (
            "the migration lane still derives a table denylist from the gateway's models; the "
            'scan it feeds cannot catch op.drop_table("users")'
        )
        assert not re.search(r"grep\s+-rn?iE.*table", body, re.IGNORECASE), (
            "the migration lane still greps migration text for table DDL"
        )


# ---------------------------------------------------------------------------
# Ownership: the chain is upstream's.
# ---------------------------------------------------------------------------


class TestChainOwnership:
    def test_the_module_ships_no_alembic_chain(self) -> None:
        """A copied chain diverges from upstream's the first time either side changes."""
        offenders = [
            path.name
            for path in MIGRATIONS_DIR.iterdir()
            if re.match(r"^[0-9a-f]{4,}[_-].*\.py$", path.name)
            or path.name == "versions"
        ]
        assert not offenders, f"an Alembic chain appeared in ADP: {offenders}"

    def test_a_copied_chain_is_refused(self, tmp_path: Path) -> None:
        """The rule is enforced, not just currently satisfied.

        Without this, the test above would keep passing while the enforcement was deleted.
        """
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "job.yaml").write_text("{}", encoding="utf-8")
        (directory / "006_add_widgets.py").write_text(
            "revision = '006'\n", encoding="utf-8"
        )

        result = _contract(migrations_dir=directory)
        assert result.returncode == 1
        assert "second chain" in result.stdout.lower() or "diverges" in result.stdout
        assert "#5045" in result.stdout or "5045" in result.stdout, (
            "the refusal must name the unit that owns the chain, or the reader's next step is "
            "to write one here"
        )

    def test_a_versions_directory_is_refused(self, tmp_path: Path) -> None:
        directory = tmp_path / "migrations"
        (directory / "versions").mkdir(parents=True)
        result = _contract(migrations_dir=directory)
        assert result.returncode == 1
        assert "chain" in result.stdout.lower()

    def test_the_lane_does_not_demand_a_chain_in_this_module(self) -> None:
        """The inverted-ownership defect, asserted at the source.

        The previous lane errored with "the repaired chain from #5045 must land in this
        module". This is the opposite requirement, so a regression to that wording is a
        regression of the ownership split.

        Comments are excluded, because the header documents the defect it replaced by quoting
        it — and a test that cannot tell a description of a mistake from the mistake forces
        the next person to delete the explanation to get green.
        """
        assert "must land in this module" not in _workflow_body()
        assert not re.search(r"migrations/versions", _workflow_body()), (
            "the lane still refers to a versions/ directory inside this module"
        )


# ---------------------------------------------------------------------------
# Unavailable inputs. Each refusal names the blocking unit.
# ---------------------------------------------------------------------------


class TestUnavailableInputsAreRefusedByName:
    def test_the_shipped_state_is_still_refused_after_the_chain_repair(self) -> None:
        """The shipped lock must still refuse — but now for the RIGHT reason.

        UPDATED BY U13 (#5045). These two tests previously asserted the refusal named the
        multi-headed chain and quoted "revision '006' declared by 3 files". The chain is
        repaired, so that refusal is gone and asserting it would pin a defect that no
        longer exists.

        What must NOT change is that the shipped state still refuses. `single_head` is now
        true, but `schema.status` is still `unverified` — no `alembic upgrade head` has run
        against a real database, because the lane that would do it is offline by design and
        the live acceptance is deferred behind an unresolved account and database access.
        An unverified head is the dangerous case the checker exists for: the first thing a
        bad head does is apply half of itself.
        """
        result = _contract()
        assert result.returncode == 1
        assert "verified" in result.stdout
        assert "half of itself" in result.stdout, (
            "the refusal should say why an unverified head is dangerous, not merely that "
            "a field has the wrong value"
        )
        assert "Nothing was changed" in result.stdout

    def test_the_repaired_chain_no_longer_refuses_on_multiple_heads(self) -> None:
        """The chain-level refusal must be gone, not merely reworded.

        Complements the test above: that one asserts the surviving refusal, this one
        asserts the retired one. Together they pin the exact transition #5045 made — the
        blocker moved from "the graph cannot resolve a target" to "the target has not been
        verified against a real database".
        """
        result = _contract()
        assert "no single head" not in result.stdout
        assert "declared by 3 files" not in result.stdout

    def test_the_reported_counts_match_the_maintained_chain(self) -> None:
        """The number in the refusal must be countable in the tree, not just plausible.

        Added by U22 (#5326), which is the first point at which this was checkable: before
        the source transfer the chain lived in a repository ADP could not read, so the lock's
        count could only be taken on faith. It was wrong — recorded as four files declaring
        ``'006'`` where three do. The four ``006_*.py`` filenames are real, but two of them
        declare their full descriptive ids and only three collide on the bare ``'006'``.

        That mattered because this count is rendered into the refusal an operator reads to
        learn what U13 must repair, so an inflated number sends them hunting a fourth
        conflicting file that does not exist. Deriving the assertion from the files means the
        lock and the chain cannot drift apart again — including after U13 repairs it.
        """
        versions = MODULE_ROOT / "src" / "superplane-api" / "alembic" / "versions"
        assert versions.is_dir(), (
            "the maintained migration chain is missing; U22 transferred it here"
        )
        declared: dict[str, int] = {}
        for path in sorted(versions.glob("*.py")):
            match = re.search(
                r'^revision(?:\s*:\s*str)?\s*=\s*["\']([^"\']+)["\']',
                path.read_text(encoding="utf-8"),
                re.M,
            )
            assert match, f"{path.name} declares no revision id"
            declared[match.group(1)] = declared.get(match.group(1), 0) + 1

        actual = {rid: n for rid, n in declared.items() if n > 1}
        observed = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))["schema"][
            "observed"
        ]
        assert observed["duplicate_revision_ids"] == actual, (
            f"the lock records {observed['duplicate_revision_ids']} but the chain has "
            f"{actual}"
        )
        assert observed["version_files"] == len(list(versions.glob("*.py")))

    def test_an_unverified_chain_is_refused_even_when_single_headed(
        self, tmp_path: Path
    ) -> None:
        lock = _unblocked_lock()
        lock["schema"]["status"] = "unverified"
        result = _contract(lock=_write_lock(tmp_path, lock))
        assert result.returncode == 1
        assert "verified" in result.stdout
        assert "half of itself" in result.stdout, (
            "the refusal should say why an unverified head is dangerous, not merely that a "
            "field has the wrong value"
        )

    def test_a_pending_runner_image_is_refused_naming_source_access(
        self, tmp_path: Path
    ) -> None:
        lock = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))
        lock["schema"]["single_head"] = True
        lock["schema"]["status"] = "verified"
        result = _contract(lock=_write_lock(tmp_path, lock))
        assert result.returncode == 1
        assert "source_access" in result.stdout
        assert "pending_images" in result.stdout
        assert "unavailable INPUT" in result.stdout, (
            "the refusal must distinguish an unavailable input from a defect in this lane, or "
            "someone will try to fix it here"
        )

    def test_a_missing_image_entry_is_refused_rather_than_guessed(
        self, tmp_path: Path
    ) -> None:
        lock = _unblocked_lock()
        lock["images"].pop("superplane-api")
        result = _contract(lock=_write_lock(tmp_path, lock))
        assert result.returncode == 1
        assert "second chain nobody reviewed" in result.stdout

    def test_a_tag_instead_of_a_digest_is_refused(self, tmp_path: Path) -> None:
        lock = _unblocked_lock()
        lock["images"]["superplane-api"] = "v1.2.3"
        result = _contract(lock=_write_lock(tmp_path, lock))
        assert result.returncode == 1
        assert "not a sha256" in result.stdout
        assert "what was applied" in result.stdout

    def test_a_pinned_digest_with_no_repository_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A digest alone is not a pullable reference."""
        lock = _unblocked_lock()
        lock["image_sources"].pop("superplane-api")
        result = _contract(lock=_write_lock(tmp_path, lock))
        assert result.returncode == 1
        assert "image_sources" in result.stdout

    def test_the_unblocked_lock_resolves_a_pinned_reference(
        self, tmp_path: Path
    ) -> None:
        """The positive case. A gate nothing can pass is not a gate.

        This is what makes the refusals above meaningful: they are conditions, not a
        permanent no.
        """
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()))
        assert result.returncode == 0, result.stdout
        assert PROMOTED_REF in result.stdout

    def test_the_image_file_is_written_only_on_success(self, tmp_path: Path) -> None:
        """A caller that ignored the exit code must not find a usable reference.

        `--emit-image` exists so the workflow does not scrape stdout; if it were written
        before the refusals, that convenience would become a bypass.
        """
        emitted = tmp_path / "image"
        blocked = _contract(emit_image=emitted)
        assert blocked.returncode == 1
        assert not emitted.exists(), "a refused run left a runner reference on disk"

        ok = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()), emit_image=emitted
        )
        assert ok.returncode == 0, ok.stdout
        assert emitted.read_text(encoding="utf-8") == PROMOTED_REF

    def test_the_refusals_are_derived_from_the_lock_not_hardcoded(self) -> None:
        """The lane must unblock by data when its owners finish.

        A hardcoded "blocked" would have to be remembered at exactly the moment everyone is
        busy celebrating that the build works.
        """
        text = CONTRACT.read_text(encoding="utf-8")
        assert 'lock.get("schema")' in text or "lock.get('schema')" in text
        assert "pending_images" in text


# ---------------------------------------------------------------------------
# Evidence, which the previous version only warned about.
# ---------------------------------------------------------------------------


class TestEvidenceIsRequiredNotWarned:
    def test_the_lane_no_longer_warns_about_backups(self) -> None:
        """`::warning::` does not stop a step, so the upgrade ran with no backup."""
        assert "::warning::" not in _workflow_body(), (
            "the migration lane still emits a warning where it needs a refusal; a warning "
            "does not stop a step, which is how the previous version reached the upgrade with "
            "no backup"
        )

    def test_a_mutating_run_with_no_evidence_is_refused(self, tmp_path: Path) -> None:
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()), require_evidence=True
        )
        assert result.returncode == 1
        assert "requires --evidence-file" in result.stdout
        assert "refusal, not a warning" in result.stdout

    def test_complete_evidence_passes(self, tmp_path: Path) -> None:
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            evidence=_evidence(tmp_path),
            require_evidence=True,
        )
        assert result.returncode == 0, result.stdout

    @pytest.mark.parametrize(
        "field", ["identifier", "endpoint", "database", "schema", "observed_by"]
    )
    def test_missing_target_fields_are_refused(
        self, field: str, tmp_path: Path
    ) -> None:
        evidence = _evidence(tmp_path, lambda payload: payload["target"].pop(field))
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            evidence=evidence,
            require_evidence=True,
        )
        assert result.returncode == 1
        assert f"target.{field}" in result.stdout

    # Both halves of the placeholder check, because they are independent code paths and a
    # test of one leaves the other free to be deleted. Found by mutation: disabling only the
    # regex half left the suite green, since every case here used a word from the literal set.
    @pytest.mark.parametrize(
        "value",
        [
            "TBD",  # the literal set
            "unknown",
            "n/a",
            "REPLACE_WITH_DB_IDENTIFIER",  # the regex — an unrendered template pasted in
            "adp-REPLACE_WITH_ENVIRONMENT-superplane-db",  # partially filled in
        ],
    )
    def test_a_placeholder_target_is_refused(self, value: str, tmp_path: Path) -> None:
        """A field that was filled in with a placeholder is not an observation.

        A presence check alone accepts every one of these, which is how a "verified target"
        record ends up meaning nothing.
        """
        evidence = _evidence(
            tmp_path, lambda payload: payload["target"].update(identifier=value)
        )
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            evidence=evidence,
            require_evidence=True,
        )
        assert result.returncode == 1
        assert "placeholder" in result.stdout

    @pytest.mark.parametrize("value", ["TBD", "REPLACE_WITH_SNAPSHOT_ID"])
    def test_a_placeholder_backup_is_refused(self, value: str, tmp_path: Path) -> None:
        """The same on the backup side, which is the field most likely to be filled in
        hurriedly to get past the gate."""
        evidence = _evidence(
            tmp_path, lambda payload: payload["backup"].update(identifier=value)
        )
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            evidence=evidence,
            require_evidence=True,
        )
        assert result.returncode == 1
        assert "placeholder" in result.stdout

    def test_a_backup_of_a_different_database_is_refused(self, tmp_path: Path) -> None:
        """The case a checkbox cannot catch, and the most likely one in practice.

        A snapshot exists, its status is available, every field is filled in — and it is of
        another instance. Nothing about the presence of a backup makes it a backup of THIS
        database.
        """
        evidence = _evidence(
            tmp_path,
            lambda payload: payload["backup"].update(
                source_identifier="adp-dev-gateway-db"
            ),
        )
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            evidence=evidence,
            require_evidence=True,
        )
        assert result.returncode == 1
        assert "different database is not a backup of this one" in result.stdout

    def test_a_backup_still_creating_is_refused(self, tmp_path: Path) -> None:
        evidence = _evidence(
            tmp_path, lambda payload: payload["backup"].update(status="creating")
        )
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            evidence=evidence,
            require_evidence=True,
        )
        assert result.returncode == 1
        assert "restored from" in result.stdout

    def test_evidence_carrying_a_credential_is_refused(self, tmp_path: Path) -> None:
        """The evidence is echoed into a step summary, so a password in it is a leak.

        Refused rather than redacted: a redaction that missed one shape would be worse than
        a refusal, and the operator can supply an endpoint without credentials.
        """
        evidence = _evidence(
            tmp_path,
            lambda payload: payload["target"].update(
                endpoint="postgresql://superplane:hunter2@db.internal:5432/superplane"
            ),
        )
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            evidence=evidence,
            require_evidence=True,
        )
        assert result.returncode == 1
        assert "inline credential" in result.stdout

    def test_no_evidence_is_required_for_a_read_only_run(self, tmp_path: Path) -> None:
        """A dry run reads. Requiring a backup to read would make the gate noise, and a gate
        that fires when it need not is a gate people route around."""
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()))
        assert result.returncode == 0, result.stdout

    def test_the_workflow_gates_evidence_on_a_mutating_run_only(self) -> None:
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["migrate"]["steps"]
        evidence_steps = [
            s for s in steps if "evidence" in (s.get("name") or "").lower()
        ]
        assert evidence_steps, "no evidence step in the migration lane"
        for step in evidence_steps:
            assert step.get("if") == "inputs.dry_run == false", (
                f"the evidence gate's condition is {step.get('if')!r}; it must fire for a "
                f"mutating run and only for one"
            )


# ---------------------------------------------------------------------------
# The boundary that replaced the scan.
# ---------------------------------------------------------------------------


class TestTheRenderedJobDeclaresTheBoundary:
    def test_the_shipped_job_renders_and_passes(self, tmp_path: Path) -> None:
        """End to end on the real file, through the real renderer, past the real check."""
        job = _render_job(tmp_path)
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 0, result.stdout

    def test_a_public_search_path_is_refused(self, tmp_path: Path) -> None:
        """THE reproduced case, at the mechanism that replaced the scan.

        With `public` resolvable, `op.drop_table("users")` reaches the gateway's table — and
        that is precisely the statement the text scan did not catch.
        """
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: _set_env(j, "PGOPTIONS", f"-c search_path={SCHEMA},public"),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "public" in result.stdout
        assert "the text scan could not catch" in result.stdout

    def test_an_absent_search_path_is_refused(self, tmp_path: Path) -> None:
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: _set_env(j, "PGOPTIONS", "-c timezone=UTC"),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "no search_path" in result.stdout

    def test_a_search_path_naming_another_schema_is_refused(
        self, tmp_path: Path
    ) -> None:
        """Not only `public`. Any extra entry is a namespace an unqualified statement reaches."""
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: _set_env(j, "PGOPTIONS", f"-c search_path={SCHEMA},gateway"),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "not exactly" in result.stdout

    def test_the_renderer_refuses_a_comma_separated_schema(
        self, tmp_path: Path
    ) -> None:
        """The same attack one layer earlier, where the generic checks cannot see it.

        `superplane,public` renders valid YAML and breaks neither the document nor the value
        — it changes the MEANING of the setting it lands in. A character blacklist cannot
        catch that, so the schema carries an explicit pattern.
        """
        output = tmp_path / "rendered"
        env = {
            **os.environ,
            "SP_NAMESPACE": NAMESPACE,
            "SP_DATABASE_SCHEMA": f"{SCHEMA},public",
            "SP_AWS_REGION": "us-east-1",
            "SP_MIGRATION_IMAGE": PROMOTED_REF,
            "SP_MIGRATION_RUN_ID": RUN_ID,
        }
        result = subprocess.run(
            [
                sys.executable,
                str(RENDERER),
                "--source-dir",
                str(MIGRATIONS_DIR),
                "--output-dir",
                str(output),
                "--lock-file",
                str(LOCK_FILE),
                "--lane",
                "migration",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 1
        assert "search_path" in result.stdout

    def test_the_default_version_table_is_refused(self, tmp_path: Path) -> None:
        """Sharing `alembic_version` means sharing the chain."""
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: _set_env(j, "ALEMBIC_VERSION_TABLE", "alembic_version"),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "re-apply their own" in result.stdout

    def test_a_version_table_outside_the_domain_schema_is_refused(
        self, tmp_path: Path
    ) -> None:
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: _set_env(j, "ALEMBIC_VERSION_TABLE_SCHEMA", "public"),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "outside the boundary" in result.stdout

    def test_a_literal_connection_uri_is_refused(self, tmp_path: Path) -> None:
        """Upstream's own db-migrate-job.yaml carries an inline password in DATABASE_URL."""
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: _set_env(
                j,
                "SUPERPLANE_DB_CONNECTION_URI",
                "postgresql+asyncpg://superplane:hunter2@postgres:5432/superplane",
            ),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "secretKeyRef" in result.stdout

    def test_an_optional_secret_reference_is_refused(self, tmp_path: Path) -> None:
        """With `optional: true` and an absent secret, the pod starts with an empty URI.

        A migration that connected to nothing must not be able to report success.
        """

        def mutate(job: dict) -> None:
            for entry in _container(job)["env"]:
                if entry.get("name") == "SUPERPLANE_DB_CONNECTION_URI":
                    entry["valueFrom"]["secretKeyRef"]["optional"] = True

        job = _mutate_job(_render_job(tmp_path), tmp_path, mutate)
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "optional: false" in result.stdout

    def test_the_default_service_account_is_refused(self, tmp_path: Path) -> None:
        """`default` has no IRSA annotation, so it is not the domain's scoped identity."""
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: j["spec"]["template"]["spec"].update(
                serviceAccountName="default"
            ),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "domain's own identity" in result.stdout

    def test_an_unpinned_runner_is_refused(self, tmp_path: Path) -> None:
        """Syntactically perfect, and not the digest the lock pins."""
        job = _mutate_job(
            _render_job(
                tmp_path,
                SP_MIGRATION_IMAGE=f"{PROMOTED_REGISTRY}/{PROMOTED_REPOSITORY}@sha256:{'ef' * 32}",
            ),
            tmp_path,
            lambda j: None,
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "the lock pins" in result.stdout

    def test_a_shell_entrypoint_is_refused(self, tmp_path: Path) -> None:
        """With `sh -c`, the arguments are source code rather than an argument vector."""
        job = _mutate_job(
            _render_job(tmp_path),
            tmp_path,
            lambda j: _container(j).update(
                command=["sh", "-c"], args=["alembic upgrade head"]
            ),
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "argument vector" in result.stdout

    def test_a_retrying_job_is_refused(self, tmp_path: Path) -> None:
        job = _mutate_job(
            _render_job(tmp_path), tmp_path, lambda j: j["spec"].update(backoffLimit=3)
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "second half" in result.stdout

    def test_the_wrong_namespace_is_refused(self, tmp_path: Path) -> None:
        job = _mutate_job(
            _render_job(tmp_path, SP_NAMESPACE="adp-gateway"), tmp_path, lambda j: None
        )
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "namespace" in result.stdout

    @pytest.mark.parametrize(
        "name",
        [
            "SUPERPLANE_DB_CONNECTION_URI",
            "PGOPTIONS",
            "ALEMBIC_VERSION_TABLE",
            "ALEMBIC_VERSION_TABLE_SCHEMA",
            "SUPERPLANE_DB_SCHEMA",
        ],
    )
    def test_a_missing_boundary_variable_is_refused(
        self, name: str, tmp_path: Path
    ) -> None:
        def mutate(job: dict) -> None:
            container = _container(job)
            container["env"] = [e for e in container["env"] if e.get("name") != name]

        job = _mutate_job(_render_job(tmp_path), tmp_path, mutate)
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert name in result.stdout

    def test_a_surviving_placeholder_is_refused(self, tmp_path: Path) -> None:
        """`kubectl` accepts a namespace of `REPLACE_WITH_NAMESPACE` without complaint."""
        job = tmp_path / "unrendered.yaml"
        job.write_text(JOB_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        result = _contract(lock=_write_lock(tmp_path, _unblocked_lock()), job_file=job)
        assert result.returncode == 1
        assert "REPLACE_WITH" in result.stdout

    def test_boundary_checks_are_not_run_without_their_expectations(
        self, tmp_path: Path
    ) -> None:
        """Passing `--job-file` with no schema must refuse, not pass vacuously.

        The comparison would be against an empty string, so every check would "hold".
        """
        job = _render_job(tmp_path)
        result = _run_contract(
            "--lock-file",
            str(_write_lock(tmp_path, _unblocked_lock())),
            "--migrations-dir",
            str(MIGRATIONS_DIR),
            "--job-file",
            str(job),
        )
        assert result.returncode == 1
        assert "vacuously" in result.stdout

    def test_the_check_states_what_it_does_not_establish(self, tmp_path: Path) -> None:
        """A passing boundary check must not read as an isolation claim.

        `search_path` constrains name resolution, not privilege, and Decision 2 is
        unresolved. The success message says so, because a green check is what people quote.
        """
        result = _contract(
            lock=_write_lock(tmp_path, _unblocked_lock()),
            job_file=_render_job(tmp_path),
        )
        assert result.returncode == 0
        assert "NAME RESOLUTION, not PRIVILEGE" in result.stdout
        assert "Decision 2" in result.stdout
        assert "no isolation, backup, retention or restore" in result.stdout.replace(
            "\n", " "
        )


# ---------------------------------------------------------------------------
# The plan is executed, not echoed.
# ---------------------------------------------------------------------------


class TestThePlanIsObtainedNotPrinted:
    def test_the_lane_no_longer_echoes_command_names_as_a_plan(self) -> None:
        """The previous step printed `alembic current` / `history` without running them, so
        the log showed a plan that was never obtained."""
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["migrate"]["steps"]
        plan = [s for s in steps if "plan" in (s.get("name") or "").lower()]
        assert plan, "no plan step in the migration lane"
        for step in plan:
            body = step.get("run") or ""
            assert not re.search(r'echo\s+"\s*alembic\s', body), (
                "the plan step echoes an alembic command name instead of running one"
            )
            assert "kubectl" in body, (
                "the plan step reaches no cluster, so it obtains nothing"
            )

    def test_the_plan_pod_is_derived_from_the_job(self, tmp_path: Path) -> None:
        """Same image, same identity, same boundary — only the arguments differ.

        A hand-written plan manifest could drift into planning a different situation than the
        one being applied, and the plan would be reassuring and wrong.
        """
        job_path = _render_job(tmp_path)
        output = tmp_path / "plan-pod.yaml"
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_POD),
                "--job-file",
                str(job_path),
                "--output",
                str(output),
                "--name-suffix",
                RUN_ID,
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        job = yaml.safe_load(job_path.read_text(encoding="utf-8"))
        pod = yaml.safe_load(output.read_text(encoding="utf-8"))
        job_container = job["spec"]["template"]["spec"]["containers"][0]
        pod_container = pod["spec"]["containers"][0]

        assert pod["kind"] == "Pod"
        assert pod["metadata"]["namespace"] == job["metadata"]["namespace"]
        assert pod_container["image"] == job_container["image"]
        assert (
            pod["spec"]["serviceAccountName"]
            == job["spec"]["template"]["spec"]["serviceAccountName"]
        )
        assert pod_container["env"] == job_container["env"], (
            "the plan pod's environment differs from the Job's, so it would read the chain "
            "state under a different boundary than the upgrade runs under"
        )

    def test_the_plan_pod_never_upgrades(self, tmp_path: Path) -> None:
        job_path = _render_job(tmp_path)
        output = tmp_path / "plan-pod.yaml"
        subprocess.run(
            [
                sys.executable,
                str(PLAN_POD),
                "--job-file",
                str(job_path),
                "--output",
                str(output),
                "--name-suffix",
                RUN_ID,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        args = yaml.safe_load(output.read_text(encoding="utf-8"))["spec"]["containers"][
            0
        ]["args"]
        assert "upgrade" not in args, f"the plan pod would mutate the schema: {args}"
        assert "current" in args

    def test_a_job_without_the_boundary_yields_no_plan_pod(
        self, tmp_path: Path
    ) -> None:
        """A plan run outside the boundary would report another schema's revision state."""

        def mutate(job: dict) -> None:
            container = _container(job)
            container["env"] = [
                e for e in container["env"] if e.get("name") != "PGOPTIONS"
            ]

        job = _mutate_job(_render_job(tmp_path), tmp_path, mutate)
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_POD),
                "--job-file",
                str(job),
                "--output",
                str(tmp_path / "out.yaml"),
                "--name-suffix",
                RUN_ID,
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "outside the schema boundary" in result.stdout
        assert not (tmp_path / "out.yaml").exists()


# ---------------------------------------------------------------------------
# The wired execution contract. The half the previous version could not satisfy.
# ---------------------------------------------------------------------------


def _stub_kubectl(
    tmp_path: Path, *, phase: str = "Succeeded", fail_on: str = ""
) -> tuple[Path, Path]:
    """A `kubectl` on PATH that records its invocations instead of reaching a cluster."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "kubectl.log"
    stub = bin_dir / "kubectl"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "with log.open('a') as handle:\n"
        "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"fail_on = {fail_on!r}\n"
        "if fail_on and fail_on in ' '.join(sys.argv[1:]):\n"
        "    sys.exit(1)\n"
        "if sys.argv[1:2] == ['get'] and '-o' in sys.argv:\n"
        f"    print({phase!r})\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, log


class TestTheLaneCanActuallyExecute:
    """Finding 6's other half: the previous lane could not run even when unblocked.

    Its apply step was `echo "::error::…"; exit 1` with no condition, so every gate could be
    satisfied and the migration still could not happen. These tests assert the wiring
    reaches `kubectl apply` — which is why they need a stub rather than only a parse.
    """

    def test_the_apply_step_has_no_unconditional_error(self) -> None:
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["migrate"]["steps"]
        apply_steps = [
            s for s in steps if (s.get("name") or "").lower().startswith("apply")
        ]
        assert apply_steps, "no apply step in the migration lane"
        for step in apply_steps:
            body = _strip_comments(step.get("run") or "")
            assert "kubectl apply" in body, (
                "the apply step does not apply anything, so the lane cannot execute its own "
                "contract even when every gate is satisfied"
            )
            # An `exit 1` guarded by a failure check is correct; an unguarded one is the defect.
            for line in body.splitlines():
                stripped = line.strip()
                if (
                    stripped.startswith('echo "::error::')
                    and "if " not in body[: body.index(line)][-200:]
                ):
                    continue
            assert not re.match(
                r"^\s*echo\s+\"::error::[^\n]*\"\s*\n\s*exit 1\s*$",
                body.strip(),
            ), "the apply step still refuses unconditionally"

    def test_apply_wiring_reaches_kubectl_with_the_rendered_job(
        self, tmp_path: Path
    ) -> None:
        """The apply step's own commands, run against a stub kubectl.

        Extracted from the workflow rather than retyped, so this cannot pass against a
        workflow that says something else.
        """
        job_path = _render_job(tmp_path)
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["migrate"]["steps"]
        apply_step = next(
            s for s in steps if (s.get("name") or "").lower().startswith("apply")
        )
        body = apply_step["run"].replace(
            "/tmp/rendered-migration/job.yaml", str(job_path)
        )

        bin_dir, log = _stub_kubectl(tmp_path)
        result = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "SP_NAMESPACE": NAMESPACE,
            },
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        invocations = log.read_text(encoding="utf-8")
        assert f"apply -f {job_path}" in invocations, (
            f"the apply step never applied the rendered Job:\n{invocations}"
        )
        assert "wait --for=condition=complete" in invocations, (
            "the apply step does not wait for completion, so a failed migration would be "
            "reported as a successful one"
        )
        assert f"--namespace {NAMESPACE}" in invocations

    def test_a_failed_job_is_reported_as_partial_not_as_success(
        self, tmp_path: Path
    ) -> None:
        """`backoffLimit: 0` means a failure is final and possibly half-applied.

        The step must say so: "the migration failed, retry it" is the wrong instruction when
        the first half already committed.
        """
        job_path = _render_job(tmp_path)
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        apply_step = next(
            s
            for s in workflow["jobs"]["migrate"]["steps"]
            if (s.get("name") or "").lower().startswith("apply")
        )
        body = apply_step["run"].replace(
            "/tmp/rendered-migration/job.yaml", str(job_path)
        )

        bin_dir, log = _stub_kubectl(tmp_path, fail_on="wait --for=condition=complete")
        result = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "SP_NAMESPACE": NAMESPACE,
            },
        )
        assert result.returncode != 0, "a failed migration Job was reported as success"
        assert "PARTIALLY MIGRATED" in result.stdout
        assert "logs" in log.read_text(encoding="utf-8"), (
            "the failure path collects no logs, so the only record of how far the migration "
            "got is discarded"
        )

    def test_the_lane_is_dispatch_only(self) -> None:
        """A schema change is the least reversible thing in this EPIC."""
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        triggers = workflow[True] if True in workflow else workflow["on"]
        assert set(triggers) == {"workflow_dispatch"}, (
            f"the migration lane must not run automatically; triggers: {list(triggers)}"
        )

    def test_every_gate_precedes_every_mutation_in_step_order(self) -> None:
        """Order is the property. A boundary check after `kubectl apply` is not a check.

        Asserted on indices rather than on presence, because a workflow can contain all the
        right steps in an order that makes them decorative.
        """
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["migrate"]["steps"]
        names = [(s.get("name") or "") for s in steps]

        def index(predicate) -> int:
            for position, step in enumerate(steps):
                if predicate(step):
                    return position
            raise AssertionError(f"no step matched; steps were {names}")

        preflight = index(lambda s: "Preflight" in (s.get("name") or ""))
        evidence = index(lambda s: "evidence" in (s.get("name") or "").lower())
        boundary = index(lambda s: "boundary" in (s.get("name") or "").lower())
        apply_at = index(lambda s: (s.get("name") or "").lower().startswith("apply"))
        confirm = index(lambda s: "Confirm" in (s.get("name") or ""))

        assert confirm == 0, "the confirmation gate must be the first step"
        assert preflight < apply_at, "the preflight runs after the apply"
        assert evidence < apply_at, "evidence is demanded after the schema is mutated"
        assert boundary < apply_at, "the boundary is verified after the Job is applied"

    def test_no_operator_input_is_interpolated_into_a_script_body(self) -> None:
        """`${{ }}` substitutes before the shell parses, so an interpolated input is source.

        `inputs.confirm` and `inputs.backup_evidence` are operator-typed, and the previous
        version put `${{ inputs.confirm }}` directly inside a `run:` body.
        """
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        offenders = []
        for step in workflow["jobs"]["migrate"]["steps"]:
            body = step.get("run") or ""
            for match in re.findall(r"\$\{\{[^}]*\}\}", body):
                if "inputs." in match or "steps." in match or "github." in match:
                    offenders.append(f"{step.get('name')}: {match}")
        assert not offenders, (
            "these values reach a script body as source code rather than as data; pass them "
            "with `env:`:\n" + "\n".join(offenders)
        )

    def test_ssm_values_reach_the_renderer_as_environment_data(self) -> None:
        """The same rule for Terraform-published values, which an operator can hand-edit."""
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        render = next(
            s
            for s in workflow["jobs"]["migrate"]["steps"]
            if "Render" in (s.get("name") or "")
        )
        assert "${{" not in _strip_comments(render.get("run") or ""), (
            "the render step interpolates into its script body"
        )
        env = render.get("env") or {}
        for required in ("SP_NAMESPACE", "SP_DATABASE_SCHEMA", "SP_MIGRATION_IMAGE"):
            assert required in env, (
                f"the render step does not pass {required} as env data"
            )
        assert "--lane migration" in render["run"], (
            "the render step does not select the migration lane, so it would supply the "
            "rollout's placeholder set"
        )

    def test_the_live_schema_parameter_is_rechecked_before_use(self) -> None:
        """`variables.tf` validates the input; SSM parameters are hand-editable afterwards.

        The value that reaches the search_path must be the constrained one, and "Terraform
        wrote it" is not a property of the live parameter.
        """
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        config = next(
            s
            for s in workflow["jobs"]["migrate"]["steps"]
            if "configuration Terraform published" in (s.get("name") or "")
        )
        # Comments stripped, and the check is on the CASE PATTERN rather than on the word
        # appearing anywhere. Found by mutation: an assertion for `"public" in step["run"]`
        # was satisfied by the comment explaining the check, so replacing the pattern with one
        # that matches nothing left the suite green.
        body = _strip_comments(config["run"])
        assert re.search(r"public\|pg_catalog\|information_schema", body), (
            "the lane reads database-schema from SSM without rechecking that it excludes "
            "`public` and the system schemas; variables.tf validates the Terraform INPUT, but "
            "the live parameter is hand-editable afterwards"
        )
        assert "exit 1" in body, (
            "the recheck does not stop the run, so a hand-edited parameter would reach the "
            "search_path anyway"
        )


# ---------------------------------------------------------------------------
# The Job manifest itself, unrendered.
# ---------------------------------------------------------------------------


class TestTheShippedJobManifest:
    JOB = yaml.safe_load(JOB_SOURCE.read_text(encoding="utf-8"))

    def test_it_declares_no_gpu_resources(self) -> None:
        """A migration needs no accelerator, and GPU work is deferred in this EPIC."""
        text = JOB_SOURCE.read_text(encoding="utf-8")
        assert "nvidia.com/gpu" not in text

    def test_it_shares_no_host_namespace(self) -> None:
        pod = self.JOB["spec"]["template"]["spec"]
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod.get(field), f"the migration Job sets {field}"

    def test_it_mounts_no_host_path(self) -> None:
        for volume in self.JOB["spec"]["template"]["spec"].get("volumes") or []:
            assert "hostPath" not in volume, (
                f"the migration Job mounts a host path: {volume}"
            )

    def test_it_contains_no_account_id_literal(self) -> None:
        """The provenance rule (acceptance 3): identities arrive from Terraform, not literals."""
        body = _strip_comments(JOB_SOURCE.read_text(encoding="utf-8"))
        assert not re.search(r"\b[0-9]{12}\b", body), (
            "a 12-digit account id is hardcoded"
        )

    def test_it_contains_no_inline_credential(self) -> None:
        body = _strip_comments(JOB_SOURCE.read_text(encoding="utf-8"))
        assert not re.search(r"://[^/\s]*:[^/@\s]+@", body)

    def test_its_image_is_a_placeholder_not_a_literal_digest(self) -> None:
        """A literal digest here would be a second pin able to disagree with the lock."""
        image = _container(self.JOB)["image"]
        assert image == "REPLACE_WITH_MIGRATION_IMAGE", (
            f"the Job names {image!r}; the digest must arrive from the lock so there is one pin"
        )

    def test_it_is_labelled_as_belonging_to_the_domain_app(self) -> None:
        """So `kubectl get -l app.kubernetes.io/part-of=adp-superplane` finds it, and so a
        teardown scoped to this module removes it."""
        assert (
            self.JOB["metadata"]["labels"]["app.kubernetes.io/part-of"]
            == "adp-superplane"
        )
        template_labels = self.JOB["spec"]["template"]["metadata"]["labels"]
        assert template_labels["app.kubernetes.io/part-of"] == "adp-superplane"

    def test_it_states_what_it_does_not_claim(self) -> None:
        """The manifest is where someone reads the boundary; the caveat belongs with it.

        Without it, `search_path=superplane` reads as isolation — and it is not, because
        privilege is granted on the database and Decision 2 is unresolved.
        """
        text = JOB_SOURCE.read_text(encoding="utf-8")
        assert "NAME RESOLUTION, not PRIVILEGE" in text
        assert "Decision 2" in text
