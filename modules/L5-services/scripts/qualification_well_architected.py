"""Read Product-selected Well-Architected work through authenticated Product APIs."""

import argparse
import base64
from datetime import UTC, datetime, timedelta
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import quote
import zipfile

import httpx

from l5_services.adapters.well_architected.qualification import ProductReader, emit_native_case, read_selected_review
from l5_services.installed_release_probe import InstalledFixture
from l5_services.wa_native_evidence import verify_native_reviews
from l5_services.source_manifest import _safe_path, _verify_artifact


ROOT = Path(__file__).resolve().parents[3]
SHA = re.compile(r"[0-9a-f]{40}\Z")
TOKEN_ENV = re.compile(r"[A-Z][A-Z0-9_]+\Z")


def private_json(path):
    selected = Path(path)
    if not selected.is_absolute() or selected.is_symlink() or selected.resolve().is_relative_to(ROOT):
        raise ValueError("private qualification input must be outside the checkout")
    descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("private qualification input has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if len(content) > 262144:
        raise ValueError("private qualification input exceeds its bound")
    return json.loads(content)


class ProductNativeSession:
    def __init__(self, selection):
        with product_connection(selection) as client:
            self.observation = ProductReader(client, selection["scope"]).get(
                "runs/" + quote(selection["run_id"], safe="") + "/native-observations")
        observed = self.observation
        if (not isinstance(observed, dict) or observed.get("schema_version") != 1
                or any(observed.get(field) != selection.get(field) for field in (
                    "run_id", "run_revision", "configuration_revision", "binding_id", "binding_revision",
                    "result_id", "result_revision", "provider_identity"))
                or any(observed.get("scope", {}).get(field) != selection["scope"].get(field)
                       for field in ("tenant_id", "product_id", "project_id"))
                or "provider_source_coverage_unverified" not in observed.get("limitations", [])):
            raise ValueError("native observations differ from selected Product scope")
        runtime = observed.get("runtime")
        if runtime is not None and (not isinstance(runtime, dict) or runtime.get("schema_version") != 1
                or runtime.get("source") != "durable-runtime" or runtime.get("run_id") != selection["run_id"]
                or runtime.get("run_revision") != selection["run_revision"]
                or runtime.get("scope") != {field: selection["scope"][field]
                                             for field in ("tenant_id", "product_id", "project_id")}):
            raise ValueError("runtime observations differ from selected Product scope")
        identity = observed["provider_identity"]
        self.account_id, self.role_arn, self.region = (identity[field] for field in ("account_id", "role_arn", "region"))
        with product_connection(selection) as client:
            verify_retained_sources(ProductReader(client, selection["scope"]), selection, observed["source_manifest"])

    def client(self, service):
        if service != "wellarchitected":
            raise ValueError("qualification permits only native Well-Architected reads")
        return self

    def _profile(self, profile):
        if profile != self.observation["native_job_ref"]["profile_arn"]:
            raise ValueError("native Profile is outside selected run")

    def get_agent_profile(self, *, profileArn):
        self._profile(profileArn)
        return self.observation["profile"]

    def list_agent_contexts(self, *, profileArn, nextToken=None):
        self._profile(profileArn)
        if nextToken is not None:
            raise ValueError("native contexts already exhaustively read")
        return {"items": self.observation["contexts"]}

    def get_agent_recommendation_generation(self, *, profileArn, generationId):
        self._profile(profileArn)
        if generationId != self.observation["native_job_ref"]["generation_id"]:
            raise ValueError("native generation is outside selected run")
        return self.observation["generation"]

    def list_agent_recommendations(self, *, profileArn, maxResults, nextToken=None):
        self._profile(profileArn)
        if nextToken is not None:
            raise ValueError("native recommendations already exhaustively read")
        return {"items": self.observation["recommendations"]}

    def get_agent_recommendation(self, *, recommendationArn):
        matching = [item for item in self.observation["recommendations"] if item.get("recommendationArn") == recommendationArn]
        if len(matching) != 1:
            raise ValueError("native recommendation is outside selected run")
        return matching[0]

    def read_archive(self, uri, reference):
        manifest = self.observation["source_manifest"]
        if uri != manifest["provider_source_uri"] or reference != manifest["archive"]:
            raise ValueError("native source archive is outside selected run")
        content = base64.b64decode(self.observation["source_archive_base64"], validate=True)
        if len(content) != reference["bytes"]:
            raise ValueError("native source archive size changed")
        return content

    def _call(self, service, operation, *arguments):
        if service != "wellarchitected":
            raise ValueError("only Well-Architected readback permitted")
        if operation == "get-agent-recommendation-generation" and len(arguments) == 4:
            return self.get_agent_recommendation_generation(profileArn=arguments[1], generationId=arguments[3])
        if operation == "get-agent-recommendation" and len(arguments) == 2:
            return self.get_agent_recommendation(recommendationArn=arguments[1])
        raise ValueError("unrecognized native readback operation")


def verify_retained_sources(product, selection, native_manifest):
    path = "runs/" + quote(selection["run_id"], safe="")
    manifest = product.get(path + "/source-manifest")
    sources = selection["sources"]
    snapshots = manifest.get("source_snapshot_ids")
    architecture = native_manifest.get("architecture") or {}
    selected_sources = architecture.get("sources") or []
    if (manifest.get("run_id") != selection["run_id"] or manifest.get("sources") != sources
            or any(manifest.get("scope", {}).get(field) != selection["scope"].get(field)
                   for field in ("tenant_id", "product_id", "project_id"))
            or any(manifest.get(field) != native_manifest.get(field)
                   for field in ("archive", "architecture", "provider_source_uri", "provider_coverage"))
            or not isinstance(snapshots, list) or len(snapshots) != len(sources)
            or len(set(snapshots)) != len(sources) or len(selected_sources) != len(sources)):
        raise ValueError("retained inputs differ from the selected native source manifest")
    for source, snapshot_id, selected in zip(sources, snapshots, selected_sources, strict=True):
        snapshot_path = path + "/source-snapshots/" + quote(snapshot_id, safe="")
        receipt = product.get(snapshot_path)
        expiry = datetime.fromisoformat(receipt["expires_at"].replace("Z", "+00:00"))
        if (receipt.get("snapshot_id") != snapshot_id or receipt.get("source_ref") != source
                or expiry.tzinfo is None or expiry <= datetime.now(UTC)
                or receipt.get("exclusions") != manifest.get("exclusions")
                or selected.get("snapshot_id") != snapshot_id or selected.get("source_ref") != source
                or selected.get("revision") != source["record_revision"]):
            raise ValueError("retained source receipt changed or expired")
        payloads = {}
        for part, field in (("manifest", "file_manifest"), ("content", "content")):
            reference = receipt[field]
            if (not reference.get("version") or reference["version"] in {"null", "latest", "HEAD"}
                    or not isinstance(reference.get("artifact_id"), str)
                    or not reference["artifact_id"].startswith("products/source-snapshots/")):
                raise ValueError("retained source artifact is not pinned")
            response = product.client.get(product.path + "/" + snapshot_path + "/" + part)
            response.raise_for_status()
            _verify_artifact(response.content, reference, 64 * 1024 * 1024)
            payloads[part] = response.content
        inventory = json.loads(payloads["manifest"])
        entries = inventory["files"]
        paths = [_safe_path(entry["path"]) for entry in entries]
        hashes = {entry["path"]: entry["sha256"] for entry in entries}
        if (inventory.get("source_ref") != source or inventory.get("exclusions") != receipt["exclusions"]
                or len(entries) != receipt["file_count"] or len(set(paths)) != len(paths)
                or sorted(paths) != selected.get("selected_paths") or hashes != selected.get("selected_hashes")
                or any(type(entry.get("bytes")) is not int or not 0 <= entry["bytes"] <= 64 * 1024 * 1024 for entry in entries)
                or sum(entry["bytes"] for entry in entries) > 64 * 1024 * 1024
                or any(entry.get("source_ref") != source or entry.get("source_revision") != source["record_revision"] for entry in entries)):
            raise ValueError("retained source inventory differs from selected coverage")
        if source["record_type"] == "document" and len(entries) == 1:
            content = {paths[0]: payloads["content"]}
        elif source["record_type"] == "repository_commit":
            try:
                with zipfile.ZipFile(io.BytesIO(payloads["content"])) as archive:
                    if sorted(archive.namelist()) != sorted(paths):
                        raise ValueError("retained repository file coverage changed")
                    content = {}
                    for entry in entries:
                        with archive.open(entry["path"]) as source_file:
                            content[entry["path"]] = source_file.read(min(entry["bytes"], 64 * 1024 * 1024) + 1)
            except zipfile.BadZipFile as error:
                raise ValueError("retained repository is not a valid archive") from error
        else:
            raise ValueError("retained source type is not supported")
        native_files = [entry for entry in architecture["files"] if entry.get("snapshot_id") == snapshot_id]
        if (len(native_files) != len(entries)
                or {entry["path"]: entry["sha256"] for entry in native_files} != hashes):
            raise ValueError("native source inventory omits retained files")
        for entry in entries:
            _verify_artifact(content[entry["path"]], entry, 64 * 1024 * 1024)


def qualify_cases(selections, fixture, *, product_factory, session_factory):
    if not isinstance(selections, list) or len(selections) != 2:
        raise ValueError("two selected Product reviews required")
    cases = []
    sessions = {}
    recommendations = {}
    archives = {}
    for selected in selections:
        scope = selected["scope"]
        session = session_factory(selected)
        if session.account_id != fixture.account_id or session.region != fixture.queue_arn.split(":")[3]:
            raise ValueError("selected provider differs from installed service identity")
        with product_factory(selected) as client:
            observed = read_selected_review(ProductReader(client, scope), session, selected)
        case = emit_native_case(observed)
        profile = case["native_job_ref"]["profile_arn"]
        if profile in sessions:
            raise ValueError("selected Product reviews share a native Profile")
        sessions[profile] = session
        archives[case["source_manifest"]["provider_source_uri"]] = session
        for item in case["result"]["recommendations"]:
            arn = item["native_id"]
            if arn in recommendations:
                raise ValueError("selected Product reviews share a native recommendation")
            recommendations[arn] = session
        cases.append(case)

    def read(service, operation, *arguments):
        if service != "wellarchitected":
            raise ValueError("only Well-Architected readback permitted")
        if operation == "get-agent-recommendation-generation":
            session = sessions[arguments[1]]
        elif operation == "get-agent-recommendation":
            session = recommendations[arguments[1]]
        else:
            raise ValueError("unrecognized native readback operation")
        return session._call("wellarchitected", operation, *arguments)

    verify_native_reviews(cases, fixture, read, read_archive=lambda uri, reference: (
        archives[uri].read_archive(uri, reference)))
    return cases


def load_selection(selection_path, release_path, source_sha):
    selected = private_json(selection_path)
    private_json(release_path)
    fixture = InstalledFixture.from_manifest(Path(release_path))
    now = datetime.now(UTC)
    expires_at = datetime.fromisoformat(selected["expires_at"].replace("Z", "+00:00"))
    if (not SHA.fullmatch(source_sha) or source_sha != fixture.source_revision
            or selected.get("source_sha") != source_sha
            or expires_at.tzinfo is None or not now < expires_at <= now + timedelta(hours=24)):
        raise ValueError("selected qualification and installed release differ or expired")
    return selected, fixture


def product_connection(case):
    token_env = case["token_env"]
    base = httpx.URL(case["gateway_url"])
    if (not TOKEN_ENV.fullmatch(token_env) or not os.environ.get(token_env)
            or base.scheme != "https" or not base.host or base.path != "/"
            or base.userinfo or base.query or base.fragment):
        raise ValueError("authorized private Product connection required")
    return httpx.Client(base_url=base, timeout=15, follow_redirects=False,
                        headers={"Authorization": "Bearer " + os.environ[token_env]})


def observe_selection(selected, fixture):
    return qualify_cases(selected["cases"], fixture, product_factory=product_connection,
                         session_factory=ProductNativeSession)


def observe_release(selected, fixture, *, read_only=False, preflight=False):
    """Collect real native/runtime readbacks and explicitly selected clients."""
    import qualification_well_architected_clients as clients
    import qualification_well_architected_observations as experiments
    import qualification_well_architected_portability as portability
    import qualification_well_architected_periodic as periodic

    native = {}

    def session(case):
        current = ProductNativeSession(case)
        native[case["run_id"]] = current
        return current

    cases = qualify_cases(selected["cases"], fixture, product_factory=product_connection, session_factory=session)
    observations = []
    for case, observed in zip(selected["cases"], cases, strict=True):
        evidence = {"run_id": case["run_id"], "scope": observed["result"]["scope"],
                    "result_revision": case["result_revision"],
                    "runtime": native[case["run_id"]].observation.get("runtime", {})}
        if "qualification_portability" in case:
            evidence["portability"] = portability.collect(case, observed, native[case["run_id"]], product_connection)
        if "qualification_experiments" in case:
            evidence["qualification_observations"] = experiments.collect(case, product_connection)
        if "qualification_periodic" in case:
            evidence["periodic"] = periodic.collect(case, product_connection)
        if "qualification_clients" in case:
            client_case = case
            if preflight:
                client_case = {**case, "qualification_clients": {key: value for key, value in case["qualification_clients"].items()
                                                                if key not in {"design_task_id", "retained_reuse"}}}
            evidence["clients"] = clients.collect(client_case, product_connection, read_only=read_only)
        observations.append(evidence)
    return cases, observations


def private_output(path):
    destination = Path(path)
    if (not destination.is_absolute() or destination.resolve().is_relative_to(ROOT)
            or not destination.parent.is_dir() or destination.parent.is_symlink()
            or destination.parent.stat().st_mode & 0o077
            or destination.is_symlink() or destination.exists()):
        raise ValueError("observation output requires a new file in an existing private directory")
    return destination


def write_private(path, document):
    destination = private_output(path)
    content = json.dumps(document, default=str) + "\n"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read selected Well-Architected Product and native evidence")
    parser.add_argument("--selection", required=True, help="private authorized Product selection JSON")
    parser.add_argument("--release", required=True, help="private installed-release fixture JSON")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", required=True, help="new private observation JSON file")
    arguments = parser.parse_args(argv)
    try:
        selected, fixture = load_selection(arguments.selection, arguments.release, arguments.source_sha)
        destination = private_output(arguments.output)
        cases = observe_selection(selected, fixture)
        write_private(destination, {"schema_version": 1, "source_sha": arguments.source_sha,
                                    "provider_coverage": "provider_coverage_unverified", "cases": cases})
        print("Well-Architected read-only observations recorded privately; live acceptance remains separate")
        return 0
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        print("Well-Architected selected qualification unavailable or incompatible", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
