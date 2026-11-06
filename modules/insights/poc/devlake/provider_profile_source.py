"""Capture real provider and Profile reads, never a pilot criterion pass receipt."""

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from provider_evidence import PROVIDERS, selection_digest


DESTINATIONS = {
    "sonarqube": ("advanced", "observability", "quality_project"),
    "jenkins": ("advanced", "pipelines", "ci_job"),
    "argocd": ("advanced", "deployments", "deployment_application"),
    "gitlab": ("basic", "repositories", "repository"),
    "pagerduty": ("advanced", "support", "incident_service"),
    "linear": ("basic", "trackers", "tracker_team"),
    "slack": ("basic", "collaboration", "communication_channel"),
}


def upstream_identity(provider, document):
    try:
        if provider == "sonarqube":
            return document["component"]["key"], document["component"]["name"]
        if provider == "jenkins":
            return document["fullName"], document["name"]
        if provider == "argocd":
            return document["metadata"]["name"], document["metadata"]["name"]
        if provider == "gitlab":
            return document["id"], document["name"]
        if provider == "pagerduty":
            return document["service"]["id"], document["service"]["name"]
        if provider == "linear":
            return document["data"]["team"]["id"], document["data"]["team"]["name"]
        if provider == "slack" and document["ok"] is True:
            return document["channel"]["id"], document["channel"]["name"]
    except (KeyError, TypeError):
        pass
    raise ValueError("provider source identity is unavailable")


def upstream_namespace(provider, document, selected):
    try:
        if provider == "argocd":
            return selected == document["spec"]["project"] + "/" + document["metadata"]["namespace"]
        if provider == "gitlab":
            return (selected == "user:" + str(document["owner"]["id"]) and
                    document["namespace"]["kind"] == "user") if selected.startswith("user:") else \
                   selected == str(document["namespace"]["id"])
        if provider == "slack" and selected != "/":
            return selected in (document["channel"].get("context_team_id"), document["channel"].get("team_id"))
    except (KeyError, TypeError):
        return False
    return True


def upstream_revisions(provider, document, namespace):
    native_id, name = upstream_identity(provider, document)
    fields = [DESTINATIONS[provider][2], namespace, str(native_id), name]
    try:
        if provider == "jenkins":
            fields.append(document["fullName"])
            document = {key: document[key] for key in ("fullName", "name", "_class")}
        elif provider == "gitlab":
            fields.append(document["path_with_namespace"])
        if not all(isinstance(value, str) and value for value in fields):
            raise ValueError()
        profile_revision = hashlib.sha256("\x00".join(fields).encode()).hexdigest()
        binding_revision = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("provider source revision is unavailable") from error
    return "observation-sha256:" + profile_revision, "observation-sha256:" + binding_revision


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, destination):
        raise ValueError("live source redirected")


def private_json(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("source input must be an absolute private file")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
            details = os.fstat(source.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or
                    stat.S_IMODE(details.st_mode) != 0o600 or details.st_size > 65536):
                raise ValueError("source input must be private and bounded")
            document = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("source input is unavailable or invalid") from error
    if not isinstance(document, dict):
        raise ValueError("source input must be an object")
    return document


def read_json(url, authorization, allowed_hosts, *, body=None):
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "https" or parsed.username or parsed.password or
            parsed.fragment or parsed.hostname not in allowed_hosts or parsed.port not in (None, 443) or
            not parsed.path):
        raise ValueError("source endpoint is not an approved HTTPS address")
    if (not isinstance(authorization, str) or
            not authorization.startswith(("Bearer ", "Basic ", "PRIVATE-TOKEN ", "Token token=", "API-KEY ")) or
            not authorization.split(" ", 1)[1]):
        raise ValueError("source access is missing")
    headers = {"Accept": "application/json"}
    if authorization.startswith("PRIVATE-TOKEN "):
        headers["PRIVATE-TOKEN"] = authorization.removeprefix("PRIVATE-TOKEN ")
    elif authorization.startswith("API-KEY "):
        if body is None or parsed.path != "/graphql":
            raise ValueError("API key source must be an approved GraphQL query")
        headers["Authorization"] = authorization.removeprefix("API-KEY ")
    else:
        headers["Authorization"] = authorization
    if body is not None:
        if parsed.path != "/graphql" or not isinstance(body, dict):
            raise ValueError("source query is not approved")
        headers["Content-Type"] = "application/json"
    try:
        with build_opener(NoRedirect()).open(
                Request(url, headers=headers, data=json.dumps(body).encode() if body is not None else None),
                timeout=30) as response:
            if response.status != 200:
                raise ValueError("live source returned a non-success response")
            raw = response.read(1024 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ValueError("live source is unavailable") from error
    if len(raw) > 1024 * 1024:
        raise ValueError("live source response is too large")
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("live source response is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("live source response must be an object")
    return document, hashlib.sha256(raw).hexdigest()


def collect(config, revision, read=None, selection=None):
    read = read or read_json
    if config.get("source_revision") != revision or not revision:
        raise ValueError("source revision does not match selected release")
    hosts, providers, gateway = (config.get(field) for field in ("allowed_hosts", "providers", "gateway"))
    if (not isinstance(hosts, list) or not hosts or
            any(not isinstance(host, str) or not host for host in hosts) or
            not isinstance(providers, dict) or set(providers) != ({"github", "jira"} | PROVIDERS if selection else {"github", "jira"}) or
            not isinstance(gateway, dict)):
        raise ValueError("approved provider sources and a Profile API are required")
    if selection is not None:
        selection_digest(selection, revision)
    observations = {}
    for provider in ("github", "jira"):
        source = providers[provider]
        if not isinstance(source, dict) or not str(source.get("native_id") or ""):
            raise ValueError("provider source identity is missing")
        upstream, digest = read(source.get("url"), source.get("authorization"), hosts)
        native_id = str(upstream.get("id") or "")
        if native_id != str(source["native_id"]) or not upstream.get("name"):
            raise ValueError("provider source identity differs from approved selection")
        observations[provider] = {"response_sha256": digest,
                                  "native_id_sha256": hashlib.sha256(native_id.encode()).hexdigest()}
    if any(not gateway.get(field) for field in ("profile_url", "bindings_url", "authorization", "product_id", "project_id")):
        raise ValueError("Profile API access or project scope is missing")
    profile, profile_digest = read(gateway["profile_url"], gateway["authorization"], hosts)
    listing, bindings_digest = read(gateway["bindings_url"], gateway["authorization"], hosts)
    basic = profile.get("configuration", {}).get("basic") if isinstance(profile.get("configuration"), dict) else None
    if (profile.get("product_id") != gateway["product_id"] or
            profile.get("project_id") != gateway["project_id"] or
            type(profile.get("revision")) is not int or profile["revision"] < 1 or
            not isinstance(listing.get("items"), list) or listing.get("next_cursor") or
            not isinstance(basic, dict) or not isinstance(profile.get("provenance"), list)):
        raise ValueError("Profile API scope or bounded binding readback is incomplete")
    for provider, field, kind in (("github", "repositories", "repository"), ("jira", "trackers", "tracker")):
        native_id = str(providers[provider]["native_id"])
        matched = [item for item in listing["items"] if isinstance(item, dict) and
                   item.get("status") == "healthy" and
                   isinstance(item.get("selection"), dict) and
                   item["selection"].get("provider") == provider and
                   item["selection"].get("kind") == kind and
                   str(item["selection"].get("native_id")) == native_id]
        refs = basic.get(field)
        if not isinstance(refs, list):
            raise ValueError("Profile API source reference is missing")
        linked = [ref for ref in refs if isinstance(ref, dict) and ref.get("record_type") == kind and
                  str(ref.get("record_id")) == native_id and ref.get("project_id") == gateway["project_id"]]
        proofs = [proof for proof in profile["provenance"] if isinstance(proof, dict) and
                  proof.get("state") == "accepted" and proof.get("method") == "connector" and
                  proof.get("coverage") == "complete" and proof.get("paths") ==
                  ["/configuration/basic/" + field] and proof.get("source_refs") == linked and
                  matched and proof.get("binding_id") == matched[0].get("id")]
        if len(matched) != 1 or len(linked) != 1 or len(proofs) != 1:
            raise ValueError("independent provider, binding and Profile API readbacks differ")
    observations["profile"] = {"response_sha256": profile_digest,
                               "bindings_sha256": bindings_digest, "revision": profile["revision"]}
    if selection is not None:
        for provider in sorted(PROVIDERS):
            current = selection["providers"][provider]
            source = providers[provider]
            if (not isinstance(source, dict) or str(source.get("native_id")) != current["record_id"] or
                    current["project_id"] != gateway["project_id"] or
                    current["record_type"] != DESTINATIONS[provider][2] or
                    not isinstance(source.get("namespace"), str) or not source["namespace"]):
                raise ValueError("approved provider selection differs from source scope")
            if provider == "linear" and read is read_json:
                upstream, digest = read_json(source.get("url"), source.get("authorization"), hosts,
                                             body={"query": "query { team(id: " + json.dumps(current["record_id"]) + ") { id name } }"})
            else:
                upstream, digest = read(source.get("url"), source.get("authorization"), hosts)
            native_id, name = upstream_identity(provider, upstream)
            if str(native_id) != current["record_id"] or not isinstance(name, str) or not name:
                raise ValueError("provider source identity differs from approved selection")
            namespace = source["namespace"]
            if not upstream_namespace(provider, upstream, namespace):
                raise ValueError("provider source namespace differs from approved selection")
            profile_revision, binding_revision = upstream_revisions(provider, upstream, namespace)
            if current["record_revision"] != profile_revision:
                raise ValueError("independent provider, binding and Profile API readbacks differ")
            section, field, kind = DESTINATIONS[provider]
            content = profile.get("configuration", {}).get(section, {})
            refs = content.get(field) if isinstance(content, dict) else None
            path = f"/configuration/{section}/{field}"
            linked = [ref for ref in refs if isinstance(ref, dict) and
                      ref.get("tenant_id") == current["tenant_id"] and
                      ref.get("project_id") == current["project_id"] and
                      ref.get("source_connection_id") == current["connection_id"] and
                      ref.get("record_type") == kind and
                      str(ref.get("record_id")) == current["record_id"] and
                      ref.get("record_revision") == current["record_revision"]] if isinstance(refs, list) else []
            matched = [item for item in listing["items"] if isinstance(item, dict) and
                       item.get("id") == current["binding_id"] and
                       item.get("project_id") == current["project_id"] and
                       item.get("revision") == current["binding_revision"] and
                       item.get("generation") == current["source_generation"] and
                       item.get("status") == "healthy" and
                       isinstance(item.get("selection"), dict) and
                       item["selection"].get("provider") == provider and
                       item["selection"].get("kind") == kind and
                       item["selection"].get("namespace") == namespace and
                       str(item["selection"].get("native_id")) == current["record_id"] and
                       item.get("source_revision") == binding_revision]
            proofs = [proof for proof in profile["provenance"] if isinstance(proof, dict) and
                      proof.get("state") == "accepted" and proof.get("method") == "connector" and
                      proof.get("coverage") == "complete" and proof.get("paths") == [path] and
                      proof.get("source_refs") == linked and matched and
                      proof.get("binding_id") == matched[0]["id"]]
            if len(linked) != 1 or len(matched) != 1 or len(proofs) != 1:
                raise ValueError("independent provider, binding and Profile API readbacks differ")
            observations[provider] = {"revision": current, "response_sha256": digest,
                                      "binding_sha256": hashlib.sha256(json.dumps(matched[0], sort_keys=True).encode()).hexdigest()}
    return {"scope": "live-source-measurement", "source_revision": revision,
            "observed_at": datetime.now(timezone.utc).isoformat(), "observations": observations,
            **({"provider_selection_digest": selection_digest(selection, revision)} if selection is not None else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read independent provider and Profile API sources")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-providers", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = private_json(args.manifest)
        if manifest.get("source_revision") != args.revision:
            raise ValueError("campaign manifest differs from source revision")
        selection = manifest.get("provider_selection")
        if args.require_providers:
            protected = json.loads(os.environ.get("ADP_DEVLAKE_PROVIDER_SELECTION", "null"))
            if selection != protected or selection is None:
                raise ValueError("protected provider selection differs from campaign manifest")
        result = collect(private_json(args.config), args.revision, selection=selection)
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(result, output, sort_keys=True)
    except (ValueError, OSError) as error:
        print(f"FAIL provider/Profile source: {error if isinstance(error, ValueError) else 'output unavailable'}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
