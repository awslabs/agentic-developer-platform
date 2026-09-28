"""Verify retained public-document bytes against exact private scan identities."""

import argparse
import bisect
import gzip
import hashlib
import io
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from verify_secret_git_objects import git, require

ALLOWED_HOSTS = {"docs.aws.amazon.com", "pricing.us-east-1.amazonaws.com"}
MAX_BYTES = 2 * 1024 * 1024


def json_values(text):
    """Map each scalar's JSON path to its exact source line, not a text match."""
    decoder = json.JSONDecoder()
    values = {}
    newlines = [index for index, character in enumerate(text) if character == "\n"]

    def whitespace(position):
        while position < len(text) and text[position].isspace():
            position += 1
        return position

    def value(position, path):
        position = whitespace(position)
        if text[position] == "{":
            keys = set()
            position = whitespace(position + 1)
            if text[position] == "}":
                return position + 1
            while True:
                key, position = decoder.raw_decode(text, position)
                require(isinstance(key, str), "Invalid JSON key")
                require(key not in keys, "Duplicate JSON key")
                keys.add(key)
                position = whitespace(position)
                require(text[position] == ":", "Missing JSON colon")
                position = whitespace(value(position + 1, path + (key,)))
                if text[position] == "}":
                    return position + 1
                require(text[position] == ",", "Missing JSON comma")
                position = whitespace(position + 1)
        if text[position] == "[":
            position = whitespace(position + 1)
            if text[position] == "]":
                return position + 1
            index = 0
            while True:
                position = whitespace(value(position, path + (index,)))
                if text[position] == "]":
                    return position + 1
                require(text[position] == ",", "Missing JSON comma")
                position = whitespace(position + 1)
                index += 1
        scalar, end = decoder.raw_decode(text, position)
        require(path not in values, "Duplicate JSON scalar path")
        values[path] = (scalar, bisect.bisect_left(newlines, position) + 1)
        return end

    end = value(0, ())
    require(not text[end:].strip(), "Trailing JSON content")
    return values


def load_document(directory, descriptor):
    url = urlsplit(descriptor["url"])
    require(
        url.scheme == "https"
        and url.hostname in ALLOWED_HOSTS
        and not any((url.username, url.password, url.query, url.fragment, url.port)),
        "Unsupported public evidence URL",
    )
    filename = descriptor["file"]
    require(
        re.fullmatch(r"public-document-[0-9]{2}\.bin\.gz", filename),
        "Invalid public artifact path",
    )
    require(
        descriptor["status"] == 200 and 0 < descriptor["bytes"] <= MAX_BYTES,
        "Invalid public response bounds",
    )
    artifact = directory / filename
    require(artifact.stat().st_size <= MAX_BYTES, "Public archive exceeds size bound")
    compressed = artifact.read_bytes()
    require(
        hashlib.sha512(compressed).hexdigest() == descriptor["compressed_sha512"],
        "Public archive integrity mismatch",
    )
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        content = stream.read(MAX_BYTES + 1)
    require(len(content) == descriptor["bytes"], "Public response length mismatch")
    require(
        hashlib.sha512(content).hexdigest() == descriptor["content_sha512"],
        "Public document integrity mismatch",
    )
    return hashlib.sha256(content).hexdigest()


def verify_context(text, record, candidate, descriptor, document_digest):
    path = tuple(record["context_path"])
    require(
        path and path[-1] in {"sha256", "source_content_sha256"},
        "Unsupported document digest field",
    )
    values = json_values(text)
    require(
        values[path] == (candidate, record["line"]), "Exact source field/line mismatch"
    )
    url_key = "url" if path[-1] == "sha256" else "source_url"
    require(
        values[path[:-1] + (url_key,)][0] == descriptor["url"],
        "Source URL is not bound to document digest",
    )
    require(candidate == document_digest, "Public bytes do not reproduce candidate")


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    require(
        records and len(records) == receipt["verified_delta"], "Receipt count mismatch"
    )
    require(
        len({r["selector"] for r in records}) == len(records),
        "Duplicate original selector",
    )
    revision = receipt["source_revision"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Invalid frozen revision")
    descriptors = receipt["public_documents"]
    require(
        len({r["file"] for r in descriptors}) == len(descriptors),
        "Duplicate public artifact",
    )
    documents = {
        d["file"]: (
            d,
            load_document(receipt_path.parent / "public-pricing-documents", d),
        )
        for d in descriptors
    }
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        digest = hashlib.sha1(group["secrets"].encode(), usedforsecurity=False).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), digest] = group["secrets"]
    blobs = {}
    for record in records:
        prefix, index = record["selector"].rsplit("|ri=", 1)
        require(
            prefix == "detect-secrets|" + record["file"] and index.isdecimal(),
            "Original selector mismatch",
        )
        original = scan[record["file"]][int(index)]
        require(
            original["line_number"] == record["line"]
            and original["type"] == record["detector"]
            and len(record["candidate_hash_prefix"]) == 16
            and original["hashed_secret"].startswith(record["candidate_hash_prefix"]),
            "Original scan identity mismatch",
        )
        candidate = candidates[
            record["file"], record["line"], original["hashed_secret"]
        ]
        if record["file"] not in blobs:
            blobs[record["file"]] = git(
                source, "show", revision + ":" + record["file"]
            ).decode()
        descriptor, digest = documents[record["public_document"]]
        verify_context(blobs[record["file"]], record, candidate, descriptor, digest)
    require(
        {r["public_document"] for r in records} == set(documents),
        "Unbound public evidence artifact",
    )
    print(
        f"Verified {len(records)} original public-document digest selectors; no values emitted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.source, args.scan, args.audit, args.receipt)
    except Exception:  # noqa: BLE001 - redact every private-input failure
        parser.exit(1, "Verification failed; private inputs and values withheld\n")
