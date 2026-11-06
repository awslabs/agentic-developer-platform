"""Bind Transform native readbacks and full acceptance to the actual release attempt."""

import argparse
import asyncio
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import httpx

from l5_services.adapters.transform import qualification_release as native
from qualification_transform_acceptance import EvidenceSet, catalog, evaluate
from qualification_transform_collectors import NativeResults
from qualification_transform_retained import InstalledRetained
from qualification_transform_journal import ReleaseJournal, private_json, write_private, fsync_directory
from release_context import authorize_engine, release_identity

INSTALLED_FIELDS = {
    "account_id", "cli_manifest_sha256", "database_head", "frontend_asset_sha256", "gateway_image_digest",
    "manifest_version", "package_version", "queue_arn", "role_arn", "schema_sha256", "source_revision",
    "wheel_sha256", "worker_code_sha256",
}


def options(fixture):
    value = fixture.get("release_acceptance", {})
    if not isinstance(value, dict) or set(value) - {"installed", "executor_isolation"}:
        raise ValueError("Transform acceptance accepts installed selectors only; never fixture verdicts")
    if "executor_isolation" in value and (value["executor_isolation"] is not True or "installed" not in value):
        raise ValueError("Executor isolation requires installed selectors and an explicit original-run selection")
    if "installed" in value:
        selected = value["installed"]
        if (not isinstance(selected, dict) or set(selected) != {"selection", "release"}
                or not isinstance(selected["release"], dict) or set(selected["release"]) != INSTALLED_FIELDS):
            raise ValueError("Transform installed observation selection is incomplete")
    return value


class Installed:
    def __init__(self, fixture, journal, evidence):
        self.journal, self.evidence, self.bootstrap = journal, evidence, "bootstrap" in fixture
        selected = options(fixture).get("installed")
        self.series = None
        if selected:
            from qualification_installed_artifacts import SnapshotSeries
            self.series = SnapshotSeries(journal.directory, selected["selection"], SimpleNamespace(**selected["release"]),
                                         journal.binding["context"], journal)

    def before(self, phase):
        if not self.series:
            return
        if self.bootstrap:
            baseline = self.journal.directory / "installed-before-native-cases.json"
            if not baseline.exists() and (phase != "run" or any(self.journal.directory.glob("transform-bootstrap-*.jsonl"))):
                raise ValueError("installed baseline missing before original Product intent; do not rebaseline retained work")
            if phase == "run":
                self.series.checkpoint("before-native-cases")
            else:
                self.series.read("before-native-cases")

    async def selected(self, selection, number, phase):
        if not self.series:
            return
        from qualification_installed_artifacts import case_identity
        selected = {**selection, "scope": selection.get("scope", selection)}
        stage = f"after-native-case-{number}"
        if phase == "run":
            self.series.checkpoint(stage, completed_case=case_identity(selected))
        elif self.series.read(stage).get("completed_case") != case_identity(selected):
            raise ValueError("installed checkpoint belongs to another original native result")

    def finish(self, cases):
        if not self.series:
            return
        if self.bootstrap and len(cases) == 2:
            value = self.series.verify(cases)
            self.evidence._collected("same-artifacts", value)
        else:
            value = self.series.collector(self.series.config, self.series.fixture, self.series.context)
        self.evidence._collected("runtime-revisions", value)


def publish_local_report(journal, report):
    # Preserve every prior report; the conventional final path is only a current
    # pointer for the trusted publisher, never an evidence source for this emitter.
    path = journal.directory / f"acceptance-report-{len(journal.records)}-{uuid4().hex}.json"
    write_private(path, report)
    destination = journal.directory / "qualification.json"
    if destination.exists():
        private_json(destination)
    temporary = journal.directory / f"qualification-{len(journal.records)}-{uuid4().hex}.tmp"
    write_private(temporary, report)
    os.replace(temporary, destination)
    fsync_directory(journal.directory)
    journal.append("report", {"path": path.name, "sha256": sha256(path.read_bytes()).hexdigest()})


async def run(source_sha, fixture_path, directory, *, phase, environment=None):
    environment = os.environ if environment is None else environment
    context = release_identity(environment)
    if context["service"] != "transform" or context["source_revision"] != source_sha:
        raise ValueError("Transform qualification differs from the validated release identity")
    authorize_engine(environment, context)
    fixture = native.load_fixture(source_sha, fixture_path)
    options(fixture)
    native.evidence_path(directory, "qualification.json")
    _, revision = catalog()
    journal = ReleaseJournal(directory, context, sha256(Path(fixture_path).read_bytes()).hexdigest(), revision)
    with journal.locked():
        # Preflight binds the attempt but performs no provider/installed probes.
        if phase == "preflight":
            return await native.run(source_sha, fixture_path, directory, phase=phase)
        facts = EvidenceSet(journal)
        cases = NativeResults(facts)
        installed = Installed(fixture, journal, facts)
        retained = InstalledRetained(facts, cases, installed,
            fixture.get("runtime", {}).get("L5_TRANSFORM_ARTIFACT_BUCKET", environment.get("L5_TRANSFORM_ARTIFACT_BUCKET")))
        for selected in fixture.get("runs", []):
            cases.select(selected)
        async def selected_case(selected, number):
            cases.select(selected)
            await installed.selected(selected, number, phase)
        journal.append("phase-started", {"phase": phase})
        try:
            installed.before(phase)
            if options(fixture).get("executor_isolation"):
                from qualification_transform_isolation import InstalledIsolation
                isolation = InstalledIsolation(facts, options(fixture)["installed"], environment=environment)
                await asyncio.to_thread(isolation.collect, phase=phase)
            await native.run(source_sha, fixture_path, directory, phase=phase,
                             on_verified_case=cases.verified,
                             on_selected=selected_case, on_retained_case=retained.collect,
                             output_name=f"selected-run-{phase}-{len(journal.records)}.json")
            cases.finish()
            installed.finish(cases.cases)
            retained.finish()
        except Exception as error:
            # Original raw diagnostics stay private. Publication gets stable
            # criterion actions; no exception/provider response is copied out.
            journal.append("collection-incomplete", {"exception_type": type(error).__name__})
            publish_local_report(journal, evaluate(facts))
            raise
        finally:
            retained.close()
        report = evaluate(facts)
        publish_local_report(journal, report)
        return report


def main(*, emitter=False):
    parser = argparse.ArgumentParser(description=__doc__)
    if not emitter:
        parser.add_argument("phase", choices=("preflight", "run"))
    parser.add_argument("source_sha")
    parser.add_argument("fixture_path")
    parser.add_argument("evidence_dir")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.source_sha, args.fixture_path, args.evidence_dir,
                                 phase="emit" if emitter else args.phase))
    except native.BootstrapIncomplete as error:
        raise SystemExit(str(error)) from None
    except httpx.HTTPStatusError as error:
        actions = {401: "Refresh the dedicated qualification session.",
                   403: "Restore current Product and source authorization for the qualification identity.",
                   404: "Verify the selected Product, project, run and installed service routes.",
                   409: "Reconcile the original request, binding and source revisions.",
                   412: "Re-read the selected configuration and preserve the existing run identity.",
                   422: "Validate the selected fixture against the installed request contract.",
                   503: "Complete executor qualification and installation before native submission."}
        raise SystemExit(f"Transform qualification HTTP {error.response.status_code}: " +
                         actions.get(error.response.status_code, "Inspect the protected Gateway logs for this request.")) from None
    except Exception:
        raise SystemExit("Transform collection incomplete; inspect the private release journal and original run; do not resubmit it") from None
    if not emitter and args.phase == "preflight":
        print("Transform configuration validated; no installed or native observations performed")
    elif report.get("unresolved"):
        for item in report["unresolved"]:
            print(item["code"] + ": " + item["next_action"])
    else:
        print("Transform release observations collected; publisher verifies final acceptance and installed revision")
