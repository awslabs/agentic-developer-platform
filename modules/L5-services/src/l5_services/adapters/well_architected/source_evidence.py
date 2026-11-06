"""Verify qualification evidence against the selected immutable source object."""

from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from l5_services.adapters.well_architected.results import _source_files


def archive_location(uri, reference):
    if not isinstance(uri, str) or not isinstance(reference, dict):
        raise ValueError("selected source object identity unavailable")
    parsed = urlsplit(uri)
    key = reference.get("artifact_id")
    if (parsed.scheme != "s3" or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", parsed.netloc)
            or parsed.query or parsed.fragment or not isinstance(key, str) or not key
            or parsed.path != "/" + key or not reference.get("version")
            or type(reference.get("bytes")) is not int or not 0 < reference["bytes"] <= 25 * 1024 * 1024):
        raise ValueError("selected source object identity unavailable")
    return parsed.netloc, key


def read_broker_archive(call, uri, reference):
    bucket, key = archive_location(uri, reference)
    arguments = ("--bucket", bucket, "--key", key, "--version-id", reference["version"])
    head = call("s3api", "head-object", *arguments)
    if head.get("VersionId") != reference["version"] or head.get("ContentLength") != reference["bytes"]:
        raise ValueError("selected source object version or size changed")
    with TemporaryDirectory(prefix="l5-wa-source-") as directory:
        path = Path(directory) / "archive.zip"
        response = call("s3api", "get-object", *arguments, str(path))
        if (response.get("VersionId") != reference["version"]
                or response.get("ContentLength") != reference["bytes"]):
            raise ValueError("selected source object version or size changed")
        with path.open("rb") as source:
            return source.read(reference["bytes"] + 1)


def read_source_files(manifest, read_archive):
    if not isinstance(manifest, dict) or not callable(read_archive):
        raise ValueError("authorized selected source reader unavailable")
    reference = manifest.get("archive")
    uri = manifest.get("provider_source_uri")
    archive_location(uri, reference)
    return _source_files(manifest, read_archive(uri, reference))


def validate_citations(citations, files, scope):
    if not isinstance(citations, list):
        raise ValueError("invalid source citations")
    for citation in citations:
        if not isinstance(citation, dict):
            raise ValueError("citation does not match pinned source line")
        matching = [source for source in files.values()
                    if all(citation.get(field) == source["entry"].get(field)
                           for field in ("source_ref", "revision", "path", "sha256"))]
        line = citation.get("line")
        if (len(matching) != 1 or type(line) is not int or line < 1
                or any(citation.get("source_ref", {}).get(field) != scope.get(field)
                       for field in ("tenant_id", "project_id"))
                or citation.get("revision") != citation.get("source_ref", {}).get("record_revision")
                or line > len(matching[0]["lines"])
                or citation.get("quote") != matching[0]["lines"][line - 1]):
            raise ValueError("citation does not match pinned source line")
